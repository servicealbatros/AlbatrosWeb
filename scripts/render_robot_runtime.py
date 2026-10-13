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
REPORT = {"phase": "starting", "messages_enabled": False, "live_data_loaded": False}


def stop(*_):
    for process in reversed(CHILDREN):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    for process in reversed(CHILDREN):
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
        profile = Path(os.getenv('ALBATROS_DATA_DIR', '/tmp/albatros-probe')) / 'linux_browser'
        profile.mkdir(parents=True, exist_ok=True)
        spawn(browser_command(playwright.chromium.executable_path, profile))
        wait_url('http://127.0.0.1:9222/json/version')
        browser = playwright.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=30000)
        context = browser.contexts[0]
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
        public_pages = [] if os.getenv('ALBATROS_PROVIDER_TEST_CONSOLE') == 'true' else [
                         ('dsm', 'https://apps.dsmpro.ro/'),
                          ('autototal', 'https://www3.eoriginal.ro/'),
                          ('intercars', 'https://ro.e-cat.intercars.eu/')]
        for name, url in public_pages:
            page = context.new_page()
            try:
                response = page.goto(url, wait_until='domcontentloaded', timeout=20000)
                reachability[name] = {'http_status': response.status if response else None,
                                      'authenticated': False}
            except Exception as error:
                reachability[name] = {'error': type(error).__name__, 'authenticated': False}
            finally:
                if not keep_provider_tabs:
                    page.close()
        REPORT.update(phase='browser_ready', chromium=True, cdp=True, intercars_extension=True,
                      provider_sessions='not_authenticated', public_provider_pages=reachability)
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


def main():
    print('Starting isolated Render runtime', flush=True)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    production = os.getenv('ALBATROS_RENDER_MODE', 'probe') == 'production'
    if production:
        os.environ.setdefault('ALBATROS_DATA_DIR', '/var/data/albatros')
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
