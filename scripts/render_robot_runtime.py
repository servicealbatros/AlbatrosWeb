"""Linux browser compatibility probe and isolated Render process supervisor.

The default is a credential-free probe. Production mode is a separate opt-in;
never import live data or enable messaging just because a browser starts.
"""
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
CHILDREN = []
PROFILE_LOCK_HANDLE = None
REPORT = {"phase": "starting", "messages_enabled": False, "live_data_loaded": False}


def stop(*_):
    for process in reversed(CHILDREN):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
    raise SystemExit(0)


def spawn(command, env=None):
    process = subprocess.Popen(command, cwd=BASE, env=env, start_new_session=True)
    CHILDREN.append(process)
    return process


def wait_url(url, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            with urlopen(url, timeout=2) as response:
                return json.load(response)
        except Exception:
            time.sleep(1)
    raise RuntimeError("Internal component did not become ready")


class ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Only non-sensitive compatibility facts are public in the probe.
        status = 200 if self.path == '/health' else 404
        body = json.dumps(REPORT if status == 200 else {"detail": "Not found"}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_response(403)
        self.end_headers()

    def log_message(self, *_):
        pass


def browser_command(executable, profile):
    extension = BASE / 'edge_robot_extension_V6_TALON_MATCH'
    return [executable, '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
            '--no-default-browser-check', '--remote-debugging-address=127.0.0.1',
            '--remote-debugging-port=9222', f'--user-data-dir={profile}',
            f'--disable-extensions-except={extension}', f'--load-extension={extension}',
            'about:blank']


def profile_storage(profile):
    profile.mkdir(parents=True, exist_ok=True)
    marker = profile / '.albatros-profile-id'
    if not marker.exists():
        marker.write_text(secrets.token_hex(16), encoding='ascii')
    return {'browser_profile_path': str(profile),
            'browser_profile_id': marker.read_text(encoding='ascii').strip(),
            'browser_profile_on_persistent_mount': os.path.ismount(profile.parent)}


def stale_lock_owner(target, current_host, service_id, process_alive):
    try:
        owner_host, owner_pid = target.rsplit('-', 1)
        owner_pid = int(owner_pid)
        if owner_pid <= 0:
            return False
    except (ValueError, TypeError):
        return False
    if owner_host == current_host:
        return not process_alive(owner_pid)
    # Render gives a disk to only one instance. Never apply this rule to an
    # arbitrary foreign host or a desktop Chrome profile.
    return bool(service_id and owner_host.startswith(service_id + '-')
                and current_host.startswith(service_id + '-'))


def lock_browser_profile(profile):
    global PROFILE_LOCK_HANDLE
    import fcntl
    handle = (profile / '.albatros-runtime.lock').open('a+b')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another owned runtime is using this browser profile') from None
    PROFILE_LOCK_HANDLE = handle
    lock = profile / 'SingletonLock'
    if not lock.is_symlink():
        return

    def process_alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    service_id = os.getenv('ALBATROS_RENDER_SINGLE_INSTANCE_ID', '')
    if not stale_lock_owner(os.readlink(lock), socket.gethostname(), service_id, process_alive):
        return
    for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
        link = profile / name
        if link.is_symlink():
            link.unlink()  # Only stale marker links, never session data or targets.
    print('Recovered stale owned Chromium profile markers', flush=True)


def probe_browser(keep_provider_tabs=False):
    from playwright.sync_api import sync_playwright
    display = ':' + str(100 + secrets.randbelow(10000))
    os.environ['DISPLAY'] = display
    display_process = spawn(['Xvfb', display, '-screen', '0', '1440x900x24', '-nolisten', 'tcp'])
    deadline = time.monotonic() + 15
    while True:
        if display_process.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('Private browser display did not start')
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.connect('/tmp/.X11-unix/X' + display[1:])
            break
        except OSError:
            pass
        time.sleep(0.2)
    with sync_playwright() as playwright:
        profile = Path(os.getenv('ALBATROS_BROWSER_PROFILE_DIR') or
                       str(Path(os.getenv('ALBATROS_DATA_DIR', '/tmp/albatros-probe')) / 'linux_browser'))
        REPORT.update(profile_storage(profile))
        lock_browser_profile(profile)
        spawn(browser_command(playwright.chromium.executable_path, profile))
        wait_url('http://127.0.0.1:9222/json/version')
        browser = playwright.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=30000)
        context = browser.contexts[0]
        restored_dsm = None
        state_key = os.getenv('ALBATROS_PROVIDER_STATE_KEY')
        if state_key:
            from dsm_session_vault import DsmSessionVault
            try:
                restored_dsm = DsmSessionVault(profile, state_key).load()
                if restored_dsm and restored_dsm['cookies']:
                    context.add_cookies(restored_dsm['cookies'])
                REPORT['dsm_session_restore'] = {'configured': True, 'restored_cookie_count': len(restored_dsm['cookies']) if restored_dsm else 0}
            except Exception as error:
                REPORT['dsm_session_restore'] = {'configured': True, 'error': type(error).__name__}
        if not context.service_workers:
            context.wait_for_event('serviceworker', timeout=30000)
        workers = context.service_workers
        extension_loaded = any(worker.url.startswith('chrome-extension://') for worker in workers)
        if not extension_loaded:
            raise RuntimeError('Inter Cars extension not loaded')
        page = context.new_page()
        page.set_content('<title>Albatros Linux probe</title><p>isolated</p>')
        if page.title() != 'Albatros Linux probe':
            raise RuntimeError('Browser commands failed')
        page.close()
        # Public landing pages only: no provider login, VIN lookup or writes.
        reachability = {}
        public_pages = [] if os.getenv('ALBATROS_PROVIDER_TEST_CONSOLE') == 'true' and not keep_provider_tabs else [
                         ('dsm', restored_dsm['landing'] if restored_dsm and restored_dsm['cookies'] else 'https://apps.dsmpro.ro/'),
                          ('autototal', 'https://www3.eoriginal.ro/'),
                          ('intercars', 'https://ro.e-cat.intercars.eu/')]
        from provider_sessions import provider_url
        provider_names = {'dsm': 'DSM', 'autototal': 'AutoTotal', 'intercars': 'Inter Cars'}
        for name, url in public_pages:
            page = next((p for p in context.pages if not p.is_closed()
                         and provider_url(provider_names[name], p.url)), None) if keep_provider_tabs else None
            existing = page is not None
            page = page or context.new_page()
            try:
                response = None if existing else page.goto(url, wait_until='domcontentloaded', timeout=20000)
                reachability[name] = {'http_status': response.status if response else None,
                                      'authenticated': False}
            except Exception as error:
                reachability[name] = {'error': type(error).__name__, 'authenticated': False}
            finally:
                if not keep_provider_tabs:
                    page.close()
        if state_key:
            os.environ.setdefault('ALBATROS_BROWSER_PROFILE_DIR', str(profile))
            spawn([sys.executable, str(BASE / 'scripts/dsm_session_keeper.py')])
        REPORT.update(phase='browser_ready', chromium=True, cdp=True, intercars_extension=True,
                      provider_sessions='unverified', public_provider_pages=reachability)
        print(json.dumps(REPORT), flush=True)
        if os.getenv('ALBATROS_RENDER_MODE') == 'production':
            path = Path(os.environ['ALBATROS_DATA_DIR']) / 'render_runtime_health.json'
            path.write_text(json.dumps(REPORT), encoding='utf-8')


def core_environment():
    env = os.environ.copy()
    env.update(ALBATROS_CLOUD_MODE='false', ALBATROS_GATEWAY_ONLY='false',
               ALBATROS_PORT='8010', ALBATROS_BIND_HOST='127.0.0.1',
               ALBATROS_TECHNICIAN_PYTHON=sys.executable,
               ALBATROS_TECHNICIAN_WORKER=str(BASE / 'technician_request_worker_v6_time_parse_fix.py'))
    # Results are persistent; executable worker source lives in /app.
    env.setdefault('ALBATROS_DATA_DIR', '/var/data/albatros')
    env.setdefault('ALBATROS_OFFERING_DIR', '/var/data/albatros/offering')
    env.pop('RECEPTION_BRIDGE_URL', None)
    return env


def prepare_operational_data():
    """Activate a verified snapshot once, without replacing existing live data."""
    archive = os.getenv('ALBATROS_MIGRATION_ARCHIVE', '')
    if not archive:
        return
    from scripts.restore_cloud_migration import restore
    root = Path(os.environ['ALBATROS_DATA_DIR']).resolve()
    source = Path(archive).resolve()
    digest = os.environ['ALBATROS_MIGRATION_SHA256']
    target = root / ('live-' + digest[:16])
    if source.parent != root / 'worker_backups' or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid migration source')
    if target.exists():
        validation = json.loads((target / 'migration_validation.json').read_text())
        if validation.get('source_sha256') != digest:
            raise ValueError('Migration validation mismatch')
    else:
        restore(source, digest, target)
    os.environ.setdefault('ALBATROS_BROWSER_PROFILE_DIR', str(root / 'linux_browser'))
    os.environ['ALBATROS_DATA_DIR'] = str(target)
    os.environ['ALBATROS_OFFERING_DIR'] = str(target / 'offering')
    REPORT.update(live_data_loaded=True, migration_counts=json.loads(
        (target / 'migration_validation.json').read_text())['counts'])


def main():
    print('Starting isolated Render runtime', flush=True)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    production = os.getenv('ALBATROS_RENDER_MODE', 'probe') == 'production'
    if production:
        os.environ.setdefault('ALBATROS_DATA_DIR', '/var/data/albatros')
        prepare_operational_data()
        if not os.getenv('RECEPTION_BRIDGE_SECRET'):
            os.environ['RECEPTION_BRIDGE_SECRET'] = secrets.token_urlsafe(48)
        core = spawn([sys.executable, str(BASE / 'scripts/render_single_app.py')], core_environment())
        wait_url('http://127.0.0.1:8010/health')
    else:
        server = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '10000'))), ProbeHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print('Compatibility probe HTTP endpoint is ready', flush=True)
    try:
        probe_browser(keep_provider_tabs=production and os.getenv('ALBATROS_KEEP_PROVIDER_TABS', 'true') == 'true')
    except Exception as error:
        REPORT.update(phase='failed', error=type(error).__name__ + ': ' + str(error)[:250])
        print(json.dumps(REPORT), flush=True)
        if production:
            stop()
    while True:
        if any(process.poll() is not None for process in CHILDREN):
            print('An owned component exited; stopping the service', flush=True)
            stop()
        time.sleep(3)


if __name__ == '__main__':
    main()
