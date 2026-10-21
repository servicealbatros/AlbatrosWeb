"""Idle-only Chromium housekeeping; never clear cookies or restart the browser.

Linux flock leases prevent maintenance racing with HTTP actions, supplier
workers and the crawler. The existing session keeper runs this code, so no
additional browser/Playwright process is needed.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager, closing
from functools import wraps
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit


def enabled():
    return (os.name == 'posix' and os.getenv('ALBATROS_RENDER_MODE') == 'production'
            and os.getenv('ALBATROS_BROWSER_MEMORY_GUARD', 'true') == 'true')


def data_root():
    return Path(os.environ['ALBATROS_DATA_DIR'])


class Lease:
    def __init__(self, root, exclusive=False):
        self.root, self.exclusive, self.handle = Path(root), exclusive, None

    def acquire(self):
        import fcntl
        if self.handle is None:
            self.handle = (self.root / '.browser-activity.lock').open('a+b')
        try:
            fcntl.flock(self.handle, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def close(self):
        if self.handle is not None:
            self.handle.close()  # Releasing the descriptor releases the flock.
            self.handle = None


@asynccontextmanager
async def browser_activity(root=None):
    if not enabled():
        yield
        return
    lease = Lease(root or data_root())
    try:
        while not lease.acquire():
            await asyncio.sleep(0.05)
        yield
    finally:
        lease.close()


@contextmanager
def browser_activity_sync():
    if not enabled():
        yield
        return
    lease = Lease(data_root())
    try:
        while not lease.acquire():
            time.sleep(0.05)
        yield
    finally:
        lease.close()


def browser_operation(function):
    @wraps(function)
    async def protected(*args, **kwargs):
        async with browser_activity():
            return await function(*args, **kwargs)
    return protected


class BrowserActivityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        browser_route = (path.startswith(('/api/provider-auth/', '/api/robot/', '/api/dsm/'))
                         or path in {'/webhook/whatsapp', '/api/chat', '/api/input', '/api/talon'}
                         or (scope.get('method') == 'POST'
                             and path.startswith(('/api/requests', '/api/dashboard/master/'))))
        if scope['type'] != 'http' or not browser_route or not enabled():
            return await self.app(scope, receive, send)
        async with browser_activity():
            # Leave a person's login/CAPTCHA interaction alone after submission.
            if scope.get('method') == 'POST' and scope.get('path', '').startswith('/api/provider-auth/'):
                (data_root() / '.browser-console-activity').touch()
            return await self.app(scope, receive, send)


def memory_sample(base=Path('/sys/fs/cgroup')):
    try:
        used = int((base / 'memory.current').read_text())
        limit = int((base / 'memory.max').read_text())
        if limit <= 0:
            return None
        return {'used_bytes': used, 'limit_bytes': limit, 'percent': round(100 * used / limit, 1)}
    except (OSError, ValueError):
        return None


def memory_health(root):
    try:
        result = json.loads((Path(root) / 'browser_memory_health.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        result = {'status': 'waiting'}
    return {**result, 'memory': memory_sample(), 'enabled': enabled()}


def busy_reason(root):
    """Persistent jobs also cover the gap between Popen and a worker's lease."""
    root = Path(root)
    try:
        jobs = root / 'robot_offer_jobs.json'
        if jobs.exists() and any(str(j.get('status', '')).upper() in
                                 {'QUEUED', 'RUNNING', 'RETRY', 'WAITING'}
                                 for j in json.loads(jobs.read_text(encoding='utf-8'))):
            return 'offering'
        for name in ('dsm_periodic_import.json', 'dsm_accepted_offers_sync.json'):
            path = root / name
            if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('status') == 'running':
                return 'importing'
        path = root / 'pre_reception_private' / 'pilot.sqlite3'
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                if db.execute("SELECT 1 FROM orders WHERE state='creating' LIMIT 1").fetchone():
                    return 'creating_order'
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return 'state_unreadable'  # Never interpret corrupt state as idle.
    return None


def home_provider(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or p.query:
        return None
    if p.hostname == 'ro.e-cat.intercars.eu' and p.path in ('/', '/ro/') and not p.fragment:
        return 'Inter Cars'
    if p.hostname == 'www3.eoriginal.ro' and p.path == '/' and p.fragment in ('', '/'):
        return 'AutoTotal'
    return None  # DSM documents, product pages, carts and Haynes are not reloaded.


async def safe_home(page):
    from provider_sessions import authenticated, active_verification
    provider = home_provider(page.url)
    if not provider or page.is_closed() or not await authenticated(page, provider):
        return False
    if await active_verification(page):
        return False
    return not await page.locator('body').evaluate(r"""body=>{
        const shown=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
        return [...body.querySelectorAll('input,textarea,[contenteditable=true],[role=dialog],.modal.show')].some(e=>{
            if(!shown(e))return false;
            if(e.matches('[role=dialog],.modal.show,[contenteditable=true]'))return true;
            if(e.type==='password'||e.type==='file')return true;
            return !['hidden','checkbox','radio','submit','button'].includes(e.type)&&!!e.value?.trim();
        });
    }""")


class BrowserMemoryGuard:
    def __init__(self, root):
        self.root = Path(root)
        self.last_gc = self.last_reload = self.idle_since = None
        self.reload_disabled = False
        self.last_action = None

    def record(self, status, **details):
        sample = memory_sample()
        if status in {'collected', 'duplicate_closed', 'home_reloaded', 'session_needs_check'}:
            self.last_action = {'status': status, 'at': time.time(), 'after': sample, **details}
        result = {'checked_at': time.time(), 'status': status, 'memory': sample, **details}
        if self.last_action:
            result['last_action'] = self.last_action
        path = self.root / 'browser_memory_health.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result), encoding='utf-8')
        temporary.replace(path)
        return result

    async def tick(self, context, now=None):
        if not enabled():
            return
        now = time.time() if now is None else now
        lease = Lease(self.root, exclusive=True)
        try:
            if not lease.acquire():
                self.idle_since = None
                return self.record('busy')
            try:
                # Browser operations may briefly wait for housekeeping, but
                # never for an unbounded reload or a dead CDP connection.
                async with asyncio.timeout(10):
                    return await self.maintain(context, now)
            except TimeoutError:
                if self.last_reload == now:
                    self.reload_disabled = True
                return self.record('timeout')
        finally:
            lease.close()

    async def maintain(self, context, now):
        reason = busy_reason(self.root)
        console = self.root / '.browser-console-activity'
        if reason or (console.exists() and now - console.stat().st_mtime < 120):
            self.idle_since = None
            return self.record('busy', reason=reason or 'login_console')
        if self.idle_since is None:
            self.idle_since = now
        before = memory_sample()
        if not before or before['percent'] < 75:
            return self.record('healthy')
        if self.last_gc is not None and now - self.last_gc < 300:
            return self.record('cooldown')
        self.last_gc = now
        collected = 0
        for page in list(context.pages):
            if page.is_closed() or not home_provider(page.url):
                continue
            session = None
            try:
                async with asyncio.timeout(6):
                    session = await context.new_cdp_session(page)
                    await session.send('HeapProfiler.collectGarbage')
                    collected += 1
            except Exception:
                pass
            finally:
                if session:
                    try:
                        async with asyncio.timeout(2):
                            await session.detach()
                    except Exception:
                        pass
        after = memory_sample()
        # At most one home-page action per 15 minutes, after a full idle
        # minute. Keep the browser, context, cookies and local storage.
        if (not self.reload_disabled and after and after['percent'] >= 85
                and now - self.idle_since >= 60
                and (self.last_reload is None or now - self.last_reload >= 900)):
            from provider_sessions import authenticated
            for page in list(context.pages):
                async with asyncio.timeout(4):
                    safe = await safe_home(page)
                if not safe:
                    continue
                provider = home_provider(page.url)
                duplicates = [p for p in context.pages if p is not page and home_provider(p.url) == provider]
                for duplicate in duplicates:
                    async with asyncio.timeout(4):
                        duplicate_safe = await safe_home(duplicate)
                    if duplicate_safe:
                        await duplicate.close()
                        self.last_reload = now
                        return self.record('duplicate_closed', provider=provider, before=before, collected=collected)
                self.last_reload = now
                try:
                    async with asyncio.timeout(8):
                        await page.reload(wait_until='domcontentloaded', timeout=5000)
                        connected = False
                        for _ in range(5):
                            connected = await authenticated(page, provider)
                            if connected:
                                break
                            await asyncio.sleep(1)
                except Exception:
                    connected = False
                self.reload_disabled = not connected
                return self.record('home_reloaded' if connected else 'session_needs_check',
                                   provider=provider, before=before, collected=collected)
        return self.record('collected', before=before, collected=collected)
