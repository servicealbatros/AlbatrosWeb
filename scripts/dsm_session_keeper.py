"""Owned session maintenance and housekeeping; a vault key is optional."""
import asyncio
import os
from pathlib import Path
import sys
import signal
import time
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dsm_session_vault import DsmSessionVault, DsmLoginVault, trusted_landing, trusted_login
from browser_resources import BrowserMemoryGuard, browser_activity
from playwright_lifecycle import CdpDriver
from provider_sessions import ProviderSessionKeeper, upstream_error, session_response_metadata


async def reconnect(page, credentials):
    from provider_sessions import reconnect as reconnect_provider
    return await reconnect_provider(page, credentials, 'DSM')


async def keepalive(page, landing):
    _, response_url, _ = await session_response_metadata(page.request, landing, timeout=15000)
    # Dispose the response body before any subsequent navigation.
    if trusted_login(response_url):
        await page.reload(wait_until='domcontentloaded', timeout=15000)
        return False
    return True


class SessionMaintenance:
    def __init__(self, profile, root, key=None):
        self.vault_error = None
        try:
            self.vault = DsmSessionVault(profile, key) if key else None
            self.login_vault = DsmLoginVault(profile, key) if key else None
        except ValueError:
            # A broken vault must be visible, but must not disable housekeeping.
            self.vault_error = 'invalid_key'
            self.vault = self.login_vault = None
            key = None
        self.root = Path(root)
        self.suppliers = ProviderSessionKeeper(profile, key)
        self.last_read = self.last_login = self.failures = 0
        self.last_error_reload = self.login_version = 0

    async def check(self, context):
        async with browser_activity():
            connected, unavailable = False, False
            configured = bool(self.login_vault and self.login_vault.path.exists())
            version = self.login_vault.path.stat().st_mtime_ns if configured else 0
            if version != self.login_version:
                self.login_version, self.last_login, self.failures = version, 0, 0
            for page in context.pages:
                landing = trusted_landing(page.url)
                if not landing or page.is_closed():
                    continue
                if await upstream_error(page):
                    unavailable = True
                    if time.monotonic() - self.last_error_reload > 60:
                        self.last_error_reload = time.monotonic()
                        await page.reload(wait_until='domcontentloaded', timeout=15000)
                    continue
                if await page.locator('input[type=password]:visible').count():
                    continue
                if not await page.locator('a[href*="logout"],a[href*="logoff"],form[action*="logout"]').count():
                    continue
                if self.vault:
                    self.vault.save(await context.cookies(), landing)
                connected, self.failures = True, 0
                if time.monotonic() - self.last_read > 600:
                    self.last_read = time.monotonic()
                    connected = await keepalive(page, landing)
                break
            result = 'connected' if connected else 'upstream_unavailable' if unavailable else 'login_required'
            if (self.login_vault and not connected and not unavailable
                    and time.monotonic() - self.last_login > (1800 if self.failures >= 3 else 60 * 2 ** self.failures)):
                credentials = self.login_vault.load()
                if credentials:
                    for page in context.pages:
                        if trusted_login(page.url):
                            self.last_login = time.monotonic()
                            result = await reconnect(page, credentials)
                            if result == 'connected':
                                self.vault.save(await context.cookies(), page.url)
                                self.failures = 0
                            elif result == 'login_failed':
                                self.failures += 1
                            break
            state = {'status': result, 'checked_at': time.time(),
                     'auto_login_configured': configured, 'failed_attempts': self.failures,
                     'vault_error': self.vault_error}
            (self.root / 'dsm_session_health.json').write_text(json.dumps(state), encoding='utf-8')
            provider_health = await self.suppliers.check(context)
            provider_health['DSM'] = state
            (self.root / 'provider_session_health.json').write_text(json.dumps(provider_health), encoding='utf-8')


async def maintenance_iteration(owner, guard, maintenance):
    try:
        context = await owner.context()
    except Exception as error:
        guard.record('browser_unavailable', error=type(error).__name__)
        return
    try:
        await maintenance.check(context)
    except Exception as error:
        print('Supplier session maintenance: ' + type(error).__name__, flush=True)
    # A failed session check or an absent vault never bypasses housekeeping.
    try:
        await guard.tick(context)
    except Exception as error:
        guard.record('check_failed', error=type(error).__name__)


async def main():
    root = Path(os.environ['ALBATROS_DATA_DIR'])
    root.mkdir(parents=True, exist_ok=True)
    maintenance = SessionMaintenance(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], root,
                                     os.getenv('ALBATROS_PROVIDER_STATE_KEY'))
    guard, owner = BrowserMemoryGuard(root), CdpDriver()
    stopping = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stopping.set)
    guard.record('starting')
    try:
        while not stopping.is_set():
            await maintenance_iteration(owner, guard, maintenance)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await owner.shutdown()
        finally:
            guard.record('stopped')


if __name__ == '__main__':
    asyncio.run(main())
