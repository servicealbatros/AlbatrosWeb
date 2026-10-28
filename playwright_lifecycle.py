"""Own a Playwright driver and CDP transport, never the attached Chromium."""
import asyncio


class CdpDriver:
    def __init__(self, endpoint='http://127.0.0.1:9222', timeout=30000, factory=None):
        self.endpoint, self.timeout, self.factory = endpoint, timeout, factory
        self.driver = self.browser = None
        self.lock = asyncio.Lock()
        self.closed = False

    async def start(self):
        async with self.lock:
            self.closed = False  # Application lifespan startup; no eager driver.

    async def _stop(self):
        driver, self.driver, self.browser = self.driver, None, None
        if driver is not None:
            # Stopping the driver disconnects CDP. browser.close()/context.close()
            # would terminate an externally supervised browser or its sessions.
            await driver.stop()

    async def context(self):
        async with self.lock:
            if self.closed:
                raise RuntimeError('CDP lifecycle has shut down')
            if self.browser is not None and self.browser.is_connected() and self.browser.contexts:
                return self.browser.contexts[0]
            await self._stop()
            if self.factory is None:
                from playwright.async_api import async_playwright
                factory = async_playwright
            else:
                factory = self.factory
            manager = factory()
            try:
                driver = await manager.start()
            except BaseException:
                # A driver startup can fail after creating its transport.
                cleanup = getattr(manager, '__aexit__', None)
                if cleanup is not None:
                    await cleanup(None, None, None)
                raise
            try:
                browser = await driver.chromium.connect_over_cdp(self.endpoint, timeout=self.timeout)
                if not browser.contexts:
                    raise RuntimeError('Owned Chromium has no persistent context')
            except BaseException:
                await driver.stop()
                raise
            self.driver, self.browser = driver, browser
            return browser.contexts[0]

    async def shutdown(self):
        async with self.lock:
            self.closed = True
            await self._stop()
