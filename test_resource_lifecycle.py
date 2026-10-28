import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright_lifecycle import CdpDriver
from provider_sessions import session_response_metadata, ProviderSessionKeeper, AUTOTOTAL_HOME
from scripts import dsm_session_keeper as keeper


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_and_login_dispose_before_navigation(self):
        for url in (AUTOTOTAL_HOME, 'https://apps.dsmpro.ro/login'):
            response = SimpleNamespace(status=200, url=url, ok=True, dispose=AsyncMock())
            page = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
            self.assertEqual((await session_response_metadata(page.request, url, timeout=10))[1], url)
            response.dispose.assert_awaited_once()
        response.url = 'https://apps.dsmpro.ro/login'
        async def reload(**_):
            self.assertEqual(response.dispose.await_count, 2)
            raise RuntimeError('navigation failed')
        page.reload = reload
        with patch.object(keeper, 'trusted_login', return_value=True), self.assertRaises(RuntimeError):
            await keeper.keepalive(page, AUTOTOTAL_HOME)
        self.assertEqual(response.dispose.await_count, 2)

    async def test_metadata_error_disposes_and_request_error_does_not_use_stale_response(self):
        class Broken:
            dispose = AsyncMock()
            @property
            def status(self):
                raise RuntimeError('metadata failed')
        response = Broken()
        request = SimpleNamespace(get=AsyncMock(return_value=response))
        with self.assertRaises(RuntimeError):
            await session_response_metadata(request, AUTOTOTAL_HOME, timeout=10)
        response.dispose.assert_awaited_once()
        request.get.side_effect = RuntimeError('request failed')
        with self.assertRaises(RuntimeError):
            await session_response_metadata(request, AUTOTOTAL_HOME, timeout=10)
        response.dispose.assert_awaited_once()

    async def test_autototal_error_response_is_released(self):
        response = SimpleNamespace(status=403, url=AUTOTOTAL_HOME, ok=False, dispose=AsyncMock())
        page = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
        with tempfile.TemporaryDirectory() as root:
            manager = ProviderSessionKeeper(Path(root) / 'profile')
            self.assertEqual(await manager.keep_autototal(page), 'login_required')
        response.dispose.assert_awaited_once()


class DriverTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_console_reconnect_releases_cached_pages(self):
        import provider_test_console as console
        with patch.object(console,'DRIVER',SimpleNamespace(context=AsyncMock(side_effect=RuntimeError()))), \
                patch.object(console,'_browser',object()),patch.object(console,'_page',object()), \
                patch.dict(console._provider_pages,{'DSM':object()},clear=True):
            with self.assertRaises(RuntimeError):
                await console.context()
            self.assertIsNone(console._browser)
            self.assertIsNone(console._page)
            self.assertFalse(console._provider_pages)

    async def test_failed_driver_start_releases_partially_created_transport(self):
        manager=SimpleNamespace(start=AsyncMock(side_effect=RuntimeError('start failed')),__aexit__=AsyncMock())
        owner=CdpDriver(factory=lambda:manager)
        with self.assertRaises(RuntimeError):
            await owner.context()
        manager.__aexit__.assert_awaited_once()
        self.assertIsNone(owner.driver)

    def factory(self, *, fail=False, empty=False):
        self.drivers, self.browsers, self.active, self.peak = [], [], 0, 0
        def create():
            browser = SimpleNamespace(is_connected=Mock(return_value=True), contexts=[] if empty else [object()], close=AsyncMock())
            async def connect(*_, **__):
                await asyncio.sleep(0)
                if fail:
                    raise RuntimeError('CDP unavailable')
                return browser
            async def stop():
                self.active -= 1
            driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(side_effect=connect)), stop=AsyncMock(side_effect=stop))
            async def start():
                self.active += 1
                self.peak = max(self.active, self.peak)
                self.drivers.append(driver)
                self.browsers.append(browser)
                await asyncio.sleep(0)
                return driver
            return SimpleNamespace(start=start)
        return create

    async def test_concurrent_initialization_owns_exactly_one_driver(self):
        owner = CdpDriver(factory=self.factory())
        results = await asyncio.gather(*(owner.context() for _ in range(40)))
        self.assertTrue(all(context is results[0] for context in results))
        self.assertEqual(len(self.drivers), 1)
        await owner.shutdown()
        self.assertEqual(self.active, 0)
        self.browsers[0].close.assert_not_awaited()

    async def test_repeated_failures_do_not_retain_drivers(self):
        owner = CdpDriver(factory=self.factory(fail=True))
        for _ in range(50):
            with self.assertRaises(RuntimeError):
                await owner.context()
            self.assertEqual(self.active, 0)
        self.assertEqual(self.peak, 1)
        self.assertEqual(len(self.drivers), 50)
        self.assertIsNone(owner.driver)
        await owner.shutdown()

    async def test_disconnect_stops_old_driver_and_shutdown_does_not_close_chromium(self):
        owner = CdpDriver(factory=self.factory())
        await owner.context()
        self.browsers[-1].is_connected.return_value = False
        await owner.context()
        self.drivers[0].stop.assert_awaited_once()
        self.assertEqual(self.peak, 1)
        await owner.shutdown()
        await owner.shutdown()
        self.assertEqual(self.active, 0)
        for browser in self.browsers:
            browser.close.assert_not_awaited()
        with self.assertRaises(RuntimeError):
            await owner.context()

    async def test_empty_context_and_cancellation_clean_initialization(self):
        owner = CdpDriver(factory=self.factory(empty=True))
        with self.assertRaises(RuntimeError):
            await owner.context()
        self.assertEqual(self.active, 0)
        gate = asyncio.Event()
        driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock()), stop=AsyncMock())
        async def connect(*a, **k):
            await gate.wait()
        driver.chromium.connect_over_cdp.side_effect = connect
        owner = CdpDriver(factory=lambda: SimpleNamespace(start=AsyncMock(return_value=driver)))
        task = asyncio.create_task(owner.context())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        driver.stop.assert_awaited_once()

    async def test_shutdown_waits_for_initialization_then_stops_the_driver(self):
        entered, release = asyncio.Event(), asyncio.Event()
        owner = CdpDriver(factory=self.factory())
        factory = owner.factory
        def blocked_factory():
            manager = factory()
            original_start = manager.start
            async def start():
                driver = await original_start()
                original_connect = driver.chromium.connect_over_cdp
                async def connect(*a, **k):
                    entered.set()
                    await release.wait()
                    return await original_connect(*a, **k)
                driver.chromium.connect_over_cdp = connect
                return driver
            manager.start = start
            return manager
        owner.factory = blocked_factory
        initialize = asyncio.create_task(owner.context())
        await entered.wait()
        shutdown = asyncio.create_task(owner.shutdown())
        release.set()
        await asyncio.gather(initialize, shutdown)
        self.assertEqual(self.active,0)
        self.assertTrue(owner.closed)

    async def test_application_shutdown_releases_console_driver(self):
        import app as core
        import provider_test_console as console
        owner = CdpDriver(factory=self.factory())
        await owner.context()
        joined=[]
        async def pending(number):
            try:
                await asyncio.Event().wait()
            finally:
                joined.append(number)
        tasks=[asyncio.create_task(pending(number)) for number in range(4)]
        await asyncio.sleep(0)
        state=SimpleNamespace(resource_cleanup_task=tasks[0])
        with patch.object(console,'DRIVER',owner), patch.object(core,'app',SimpleNamespace(state=state)), \
                patch.dict(core._buffer_task_by_sender,{'fixture':tasks[1]},clear=True), \
                patch.object(core,'_crawler_task',tasks[2]),patch.object(core,'_whatsapp_processing_tasks',{tasks[3]}):
            await core.shutdown()
        self.assertEqual(sorted(joined),list(range(4)))
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertEqual(self.active,0)
        self.browsers[0].close.assert_not_awaited()


class HousekeepingTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_vault_key_does_not_disable_housekeeping(self):
        with tempfile.TemporaryDirectory() as root:
            maintenance = keeper.SessionMaintenance(Path(root)/'profile',root,key='invalid')
            self.assertEqual(maintenance.vault_error,'invalid_key')
            guard=SimpleNamespace(tick=AsyncMock(),record=Mock())
            await keeper.maintenance_iteration(SimpleNamespace(context=AsyncMock(return_value=SimpleNamespace(pages=[]))),
                                               guard,maintenance)
            guard.tick.assert_awaited_once()

    async def test_no_vault_and_failed_session_check_still_run_housekeeping(self):
        with tempfile.TemporaryDirectory() as root:
            maintenance = keeper.SessionMaintenance(Path(root) / 'profile', root, key=None)
            self.assertIsNone(maintenance.vault)
            context = SimpleNamespace(pages=[])
            owner = SimpleNamespace(context=AsyncMock(return_value=context))
            guard = SimpleNamespace(tick=AsyncMock(), record=Mock())
            await keeper.maintenance_iteration(owner, guard, maintenance)
            guard.tick.assert_awaited_once_with(context)
            with patch.object(maintenance, 'check', AsyncMock(side_effect=RuntimeError('supplier failed'))):
                await keeper.maintenance_iteration(owner, guard, maintenance)
            self.assertEqual(guard.tick.await_count, 2)

    async def test_cdp_failure_records_real_check_failure(self):
        guard = SimpleNamespace(tick=AsyncMock(), record=Mock())
        owner = SimpleNamespace(context=AsyncMock(side_effect=RuntimeError()))
        await keeper.maintenance_iteration(owner, guard, None)
        guard.tick.assert_not_awaited()
        guard.record.assert_called_once_with('browser_unavailable', error='RuntimeError')


class HealthAndRoleTests(unittest.TestCase):
    def test_health_distinguishes_enabled_from_fresh_running_checks(self):
        import browser_resources as resources
        with tempfile.TemporaryDirectory() as root, patch.object(resources,'enabled',return_value=True), \
                patch.object(resources,'memory_sample',return_value=None):
            self.assertFalse(resources.memory_health(root)['running'])
            guard = resources.BrowserMemoryGuard(root)
            with patch.object(resources.time,'time',return_value=100):
                guard.record('healthy')
                health = resources.memory_health(root)
                self.assertTrue(health['running'])
                self.assertEqual(health['last_checked_at'],100)
            with patch.object(resources.time,'time',return_value=221):
                self.assertFalse(resources.memory_health(root)['running'])
            guard.record('stopped')
            self.assertFalse(resources.memory_health(root)['running'])

    def test_runtime_starts_one_maintenance_process_without_vault(self):
        import test_render_runtime
        runtime = test_render_runtime.runtime
        with patch.dict(runtime.os.environ, {'ALBATROS_RENDER_MODE':'production'},clear=True), \
                patch.object(runtime,'spawn') as spawn:
            runtime.start_maintenance(Path('profile'))
            spawn.assert_called_once()
            self.assertTrue(spawn.call_args.args[0][-1].endswith('dsm_session_keeper.py'))

    def test_gateway_role_avoids_chromium_and_robot_false_preserves_robot_maintenance(self):
        import test_render_runtime
        runtime = test_render_runtime.runtime
        with patch.dict(runtime.os.environ,{'ALBATROS_RUNTIME_ROLE':'gateway'},clear=True), \
                patch.object(runtime.signal,'signal'), patch.object(runtime,'spawn') as spawn, \
                patch.object(runtime,'probe_browser') as browser, patch.object(runtime,'CHILDREN',[]), \
                patch.object(runtime.time,'sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runtime.main()
            browser.assert_not_called()
            spawn.assert_called_once()
            self.assertIn('cloud_app:app',spawn.call_args.args[0])
        with patch.dict(runtime.os.environ,{'ALBATROS_RENDER_MODE':'production','ROBOT_ENABLED':'false'},clear=True):
            self.assertEqual(runtime.runtime_role(),'robot')


if __name__ == '__main__':
    unittest.main()
