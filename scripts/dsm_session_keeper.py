"""Preserve DSM sessions and reconnect only to explicitly trusted login forms."""
import asyncio
import os
from pathlib import Path
import sys
import signal
import time
import json
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dsm_session_vault import DsmSessionVault, DsmLoginVault, trusted_landing, trusted_login

async def reconnect(page, credentials):
    from provider_sessions import reconnect as reconnect_provider
    return await reconnect_provider(page, credentials, 'DSM')

async def main():
    from playwright.async_api import async_playwright
    vault = DsmSessionVault(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], os.environ['ALBATROS_PROVIDER_STATE_KEY'])
    login_vault = DsmLoginVault(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], os.environ['ALBATROS_PROVIDER_STATE_KEY'])
    status_path = Path(os.environ['ALBATROS_DATA_DIR']) / 'dsm_session_health.json'
    last_read, last_login, failures = 0, 0, 0
    last_error_reload, login_version = 0, 0
    from provider_sessions import ProviderSessionKeeper, upstream_error
    supplier_keeper = ProviderSessionKeeper(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], os.environ['ALBATROS_PROVIDER_STATE_KEY'])
    stopping = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp('http://127.0.0.1:9222')
        context = browser.contexts[0]
        while True:
            try:
                connected = False
                unavailable = False
                version = login_vault.path.stat().st_mtime_ns if login_vault.path.exists() else 0
                if version != login_version:
                    login_version, last_login, failures = version, 0, 0
                for page in context.pages:
                    landing = trusted_landing(page.url)
                    if not landing:
                        continue
                    if await upstream_error(page):
                        unavailable = True
                        if time.monotonic() - last_error_reload > 60:
                            last_error_reload = time.monotonic()
                            await page.reload(wait_until='domcontentloaded', timeout=15000)
                        continue
                    if await page.locator('input[type=password]:visible').count():
                        continue
                    if not await page.locator('a[href*="logout"],a[href*="logoff"],form[action*="logout"]').count():
                        continue
                    vault.save(await context.cookies(), landing)
                    connected = True
                    failures = 0
                    if time.monotonic() - last_read > 600:
                        last_read = time.monotonic()
                        # Only a read of the existing home page; no DSM writes.
                        response = await page.request.get(landing, timeout=15000)
                        if trusted_login(response.url):
                            await page.reload(wait_until='domcontentloaded', timeout=15000)
                            connected = False
                    break
                result = 'connected' if connected else 'upstream_unavailable' if unavailable else 'login_required'
                if not connected and not unavailable and time.monotonic() - last_login > (1800 if failures >= 3 else 60 * 2 ** failures):
                    credentials = login_vault.load()
                    if credentials:
                        for page in context.pages:
                            if trusted_login(page.url):
                                last_login = time.monotonic()
                                result = await reconnect(page, credentials)
                                if result == 'connected':
                                    vault.save(await context.cookies(), page.url)
                                    failures = 0
                                elif result == 'login_failed':
                                    failures += 1
                                break
                status_path.write_text(json.dumps({'status': result, 'checked_at': time.time(),
                    'auto_login_configured': login_vault.path.exists(), 'failed_attempts': failures}), encoding='utf-8')
                provider_health = await supplier_keeper.check(context)
                provider_health['DSM'] = {'status': result, 'auto_login_configured': login_vault.path.exists(), 'checked_at': time.time()}
                (status_path.parent / 'provider_session_health.json').write_text(json.dumps(provider_health), encoding='utf-8')
            except Exception as error:
                print('DSM session snapshot: ' + type(error).__name__, flush=True)
            if stopping.is_set():
                break
            try:
                await asyncio.wait_for(stopping.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

if __name__ == '__main__':
    asyncio.run(main())
