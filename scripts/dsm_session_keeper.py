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
    if not credentials or not trusted_login(page.url):
        return 'login_required'
    if await page.locator('iframe[src*="recaptcha"],iframe[src*="hcaptcha"],[name="g-recaptcha-response"]').count():
        return 'human_verification_required'
    password = page.locator('input[type=password]:visible')
    username = page.locator('input[type=email]:visible,input[type=text]:visible')
    if await password.count() != 1 or await username.count() != 1:
        return 'unsupported_login_form'
    # Do not overwrite a person who is currently completing the login form.
    if await password.input_value():
        return 'human_login_in_progress'
    form = password.locator('xpath=ancestor::form')
    button = form.get_by_role('button', name=re.compile(r'^(Autentificare|Login|Log in|Conectare)$', re.I))
    if await button.count() != 1:
        return 'unsupported_login_form'
    await username.fill(credentials['username'], timeout=10000)
    await password.fill(credentials['password'], timeout=10000)
    await button.click(timeout=15000)
    for _ in range(10):
        if trusted_landing(page.url) and not await page.locator('input[type=password]:visible').count():
            if await page.locator('a[href*="logout"],a[href*="logoff"],form[action*="logout"]').count():
                return 'connected'
        await asyncio.sleep(1)
    if trusted_login(page.url):
        await password.fill('', timeout=3000)
    return 'login_failed'

async def main():
    from playwright.async_api import async_playwright
    vault = DsmSessionVault(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], os.environ['ALBATROS_PROVIDER_STATE_KEY'])
    login_vault = DsmLoginVault(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], os.environ['ALBATROS_PROVIDER_STATE_KEY'])
    status_path = Path(os.environ['ALBATROS_DATA_DIR']) / 'dsm_session_health.json'
    last_read, last_login, failures = 0, 0, 0
    stopping = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp('http://127.0.0.1:9222')
        context = browser.contexts[0]
        while True:
            try:
                connected = False
                for page in context.pages:
                    landing = trusted_landing(page.url)
                    if not landing or await page.locator('input[type=password]:visible').count():
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
                result = 'connected' if connected else 'login_required'
                if not connected and time.monotonic() - last_login > 900 and failures < 3:
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
