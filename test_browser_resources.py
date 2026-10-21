import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import browser_resources as resources


HIGH = {'used_bytes': 1900, 'limit_bytes': 2000, 'percent': 95}
LOW = {'used_bytes': 900, 'limit_bytes': 2000, 'percent': 45}


def page(url='https://ro.e-cat.intercars.eu/ro/'):
    return SimpleNamespace(url=url, is_closed=lambda: False, reload=AsyncMock(), close=AsyncMock())


class ResourceStateTests(unittest.TestCase):
    def test_only_supplier_home_pages_are_candidates(self):
        self.assertEqual(resources.home_provider('https://ro.e-cat.intercars.eu/ro/'), 'Inter Cars')
        self.assertEqual(resources.home_provider('https://www3.eoriginal.ro/#/'), 'AutoTotal')
        for url in ['https://ro.e-cat.intercars.eu/ro/p/product', 'https://www3.eoriginal.ro/#/cart',
                    'https://www3.eoriginal.ro/?search=part', 'https://evil.test/ro/',
                    'http://ro.e-cat.intercars.eu/ro/', 'https://user@www3.eoriginal.ro/',
                    'https://s.servicemanager.q-service.ro/special/home/index']:
            self.assertIsNone(resources.home_provider(url), url)

    def test_queued_worker_and_corrupt_state_are_never_idle(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'robot_offer_jobs.json'
            self.assertIsNone(resources.busy_reason(folder))
            for value in [[{'status': 'QUEUED'}], [{'status': 'RUNNING'}], [{'status': 'RETRY'}]]:
                path.write_text(json.dumps(value))
                self.assertEqual(resources.busy_reason(folder), 'offering')
            path.write_text('broken')
            self.assertEqual(resources.busy_reason(folder), 'state_unreadable')

    def test_unlimited_cgroup_is_not_treated_as_memory_pressure(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'memory.current').write_text('1024')
            (root / 'memory.max').write_text('max')
            self.assertIsNone(resources.memory_sample(root))

    @unittest.skipUnless(os.name == 'posix', 'Real Linux flock, also run in the Render image')
    def test_shared_work_and_exclusive_maintenance_cannot_overlap(self):
        with TemporaryDirectory() as folder:
            worker = resources.Lease(folder)
            maintenance = resources.Lease(folder, exclusive=True)
            other_worker = resources.Lease(folder)
            try:
                self.assertTrue(worker.acquire())
                self.assertFalse(maintenance.acquire())
                self.assertTrue(other_worker.acquire())
                worker.close()
                other_worker.close()
                self.assertTrue(maintenance.acquire())
                self.assertFalse(worker.acquire())
                maintenance.close()
                self.assertTrue(worker.acquire())
            finally:
                worker.close(); maintenance.close(); other_worker.close()


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.guard = resources.BrowserMemoryGuard(self.folder.name)
        self.guard.idle_since = 1
        self.lease = Mock(acquire=Mock(return_value=True))
        self.client = SimpleNamespace(send=AsyncMock(), detach=AsyncMock())
        self.first = page()
        self.context = SimpleNamespace(pages=[self.first], new_cdp_session=AsyncMock(return_value=self.client))
        for patcher in [patch.object(resources, 'enabled', return_value=True),
                        patch.object(resources, 'Lease', return_value=self.lease),
                        patch.object(resources, 'memory_sample', return_value=HIGH)]:
            patcher.start(); self.addCleanup(patcher.stop)

    async def test_busy_or_inflight_request_never_touches_pages(self):
        self.lease.acquire.return_value = False
        result = await self.guard.tick(self.context, now=1000)
        self.assertEqual(result['status'], 'busy')
        self.context.new_cdp_session.assert_not_awaited()
        self.first.reload.assert_not_awaited()
        self.lease.close.assert_called_once()
        self.lease.acquire.return_value = True
        (Path(self.folder.name) / 'robot_offer_jobs.json').write_text('[{"status":"QUEUED"}]')
        await self.guard.tick(self.context, now=1001)
        self.context.new_cdp_session.assert_not_awaited()

    async def test_garbage_collection_detaches_and_avoids_reload_when_enough(self):
        with patch.object(resources, 'memory_sample', side_effect=[HIGH, LOW, LOW]):
            result = await self.guard.tick(self.context, now=1000)
        self.assertEqual(result['status'], 'collected')
        self.client.send.assert_awaited_once_with('HeapProfiler.collectGarbage')
        self.client.detach.assert_awaited_once()
        self.first.reload.assert_not_awaited()
        self.first.close.assert_not_awaited()

    async def test_only_safe_home_reloads_and_action_survives_cooldown(self):
        document = page('https://s.servicemanager.q-service.ro/special/transaction/123')
        self.context.pages.insert(0, document)
        async def eligible(p):
            return p is self.first
        with patch.object(resources, 'safe_home', side_effect=eligible), \
                patch('provider_sessions.authenticated', AsyncMock(return_value=True)):
            result = await self.guard.tick(self.context, now=1000)
            second = await self.guard.tick(self.context, now=1015)
        self.assertEqual(result['status'], 'home_reloaded')
        self.assertEqual(second['status'], 'cooldown')
        self.assertEqual(second['last_action']['status'], 'home_reloaded')
        self.first.reload.assert_awaited_once()
        document.reload.assert_not_awaited()
        document.close.assert_not_awaited()

    async def test_login_or_captcha_and_human_forms_are_not_reloaded(self):
        with patch.object(resources, 'safe_home', AsyncMock(return_value=False)):
            result = await self.guard.tick(self.context, now=1000)
        self.assertEqual(result['status'], 'collected')
        self.first.reload.assert_not_awaited()

    async def test_only_duplicate_safe_home_closes_and_one_survives(self):
        duplicate = page()
        self.context.pages.append(duplicate)
        with patch.object(resources, 'safe_home', AsyncMock(return_value=True)):
            result = await self.guard.tick(self.context, now=1000)
        self.assertEqual(result['status'], 'duplicate_closed')
        duplicate.close.assert_awaited_once()
        self.first.close.assert_not_awaited()
        self.first.reload.assert_not_awaited()

    async def test_reload_failure_disables_more_automatic_reloads(self):
        self.first.reload.side_effect = RuntimeError('network unavailable')
        with patch.object(resources, 'safe_home', AsyncMock(return_value=True)):
            first = await self.guard.tick(self.context, now=1000)
            await self.guard.tick(self.context, now=2000)
        self.assertEqual(first['status'], 'session_needs_check')
        self.assertTrue(self.guard.reload_disabled)
        self.first.reload.assert_awaited_once()

    async def test_collection_failure_still_detaches(self):
        self.client.send.side_effect = RuntimeError('closed target')
        with patch.object(resources, 'safe_home', AsyncMock(return_value=False)):
            await self.guard.tick(self.context, now=1000)
        self.client.detach.assert_awaited_once()
        self.lease.close.assert_called_once()

    async def test_maintenance_timeout_releases_exclusive_lease(self):
        with patch.object(self.guard, 'maintain', AsyncMock(side_effect=TimeoutError)):
            result = await self.guard.tick(self.context, now=1000)
        self.assertEqual(result['status'], 'timeout')
        self.lease.close.assert_called_once()


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_dashboard_reads_do_not_prevent_idle_maintenance(self):
        backend = AsyncMock()
        middleware = resources.BrowserActivityMiddleware(backend)
        with patch.object(resources, 'enabled', return_value=True), patch.object(resources, 'Lease') as lease:
            await middleware({'type': 'http', 'path': '/api/dashboard/master/status', 'method': 'GET'}, None, None)
        lease.assert_not_called()
        backend.assert_awaited_once()

    async def test_interactive_browser_action_holds_shared_lease(self):
        lease = Mock(acquire=Mock(return_value=True))
        async def backend(*_):
            lease.close.assert_not_called()
            self.assertTrue(lease.acquire.called)
        middleware = resources.BrowserActivityMiddleware(backend)
        with patch.object(resources, 'enabled', return_value=True), patch.object(resources, 'data_root', return_value=Path('.')), \
                patch.object(resources, 'Lease', return_value=lease):
            await middleware({'type': 'http', 'path': '/api/requests/40/offer-start', 'method': 'POST'}, None, None)
        lease.close.assert_called_once()


class HomeSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_verification_or_edited_input_blocks_maintenance(self):
        p = page()
        locator = SimpleNamespace(evaluate=AsyncMock(return_value=True))
        p.locator = lambda _: locator
        with patch('provider_sessions.authenticated', AsyncMock(return_value=True)), \
                patch('provider_sessions.active_verification', AsyncMock(return_value=True)):
            self.assertFalse(await resources.safe_home(p))
            locator.evaluate.assert_not_awaited()
        with patch('provider_sessions.authenticated', AsyncMock(return_value=True)), \
                patch('provider_sessions.active_verification', AsyncMock(return_value=False)):
            self.assertFalse(await resources.safe_home(p))
            locator.evaluate.return_value = False
            self.assertTrue(await resources.safe_home(p))


if __name__ == '__main__':
    unittest.main()
