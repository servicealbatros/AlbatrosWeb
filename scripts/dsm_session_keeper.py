"""Capture only authenticated DSM cookies, locally; no keep-alive HTTP requests."""
import asyncio
import os
from pathlib import Path
import sys
import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dsm_session_vault import DsmSessionVault, trusted_landing

async def main():
    from playwright.async_api import async_playwright
    vault = DsmSessionVault(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], os.environ['ALBATROS_PROVIDER_STATE_KEY'])
    stopping = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp('http://127.0.0.1:9222')
        context = browser.contexts[0]
        while True:
            try:
                for page in context.pages:
                    landing = trusted_landing(page.url)
                    if not landing or await page.locator('input[type=password]:visible').count():
                        continue
                    if not await page.locator('a[href*="logout"],a[href*="logoff"],form[action*="logout"]').count():
                        continue
                    vault.save(await context.cookies(), landing)
                    break
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
