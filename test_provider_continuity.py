"""Session preservation without live supplier requests or customer changes."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urljoin
import os

from cryptography.fernet import Fernet
import provider_sessions as sessions
from scripts import dsm_session_keeper as dsm_keeper
from test_provider_sessions import login_page


def response(url, status=200):
    return SimpleNamespace(url=url, status=status, ok=200 <= status < 300, dispose=AsyncMock())


class SessionReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_errors_and_login_redirects_always_release_response(self):
        for provider, home in [('DSM', 'https://s.servicemanager.q-service.ro/special/home/index'),
                               *sessions.SUPPLIER_HOMES.items()]:
            for status, destination, expected in [
                    (200, home, 'ok'), (401, home, 'login_required'),
                    (403, home, 'login_required'), (503, home, 'upstream_unavailable'),
                    (200, urljoin(home, '/login'), 'login_required'),
                    (200, 'https://untrusted.example/login', 'login_required')]:
                with self.subTest(provider=provider, status=status, destination=destination):
                    item = response(destination, status)
                    page = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=item)))
                    result = await sessions.read_session_status(page, home, provider)
                    self.assertEqual(result, expected)
                    item.dispose.assert_awaited_once()
                    page.request.get.assert_awaited_once_with(home, timeout=10000)

    async def test_response_is_disposed_even_when_metadata_inspection_raises(self):
        class BrokenResponse:
            dispose = AsyncMock()

            @property
            def url(self):
                raise RuntimeError('metadata unavailable')

        item = BrokenResponse()
        page = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=item)))
        with self.assertRaises(RuntimeError):
            await sessions.read_session_status(page, sessions.INTERCARS_HOME, 'Inter Cars')
        item.dispose.assert_awaited_once()

    async def test_untrusted_read_target_is_rejected_before_network(self):
        page = SimpleNamespace(request=SimpleNamespace(get=AsyncMock()))
        with self.assertRaises(ValueError):
            await sessions.read_session_status(page, 'https://attacker.example/', 'Inter Cars')
        page.request.get.assert_not_awaited()

    async def test_repeated_reads_leave_no_retained_responses(self):
        retained = set()
        async def fetch(*_, **__):
            item = response(sessions.INTERCARS_HOME)
            key = id(item)
            retained.add(key)
            item.dispose = AsyncMock(side_effect=lambda: retained.remove(key))
            return item

        page = SimpleNamespace(request=SimpleNamespace(get=fetch))
        for _ in range(200):
            self.assertEqual(await sessions.read_session_status(page, sessions.INTERCARS_HOME, 'Inter Cars'), 'ok')
            self.assertEqual(retained, set())


class SupplierContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.keeper = sessions.ProviderSessionKeeper(Path(self.folder.name) / 'profile', Fernet.generate_key().decode())

    async def test_intercars_keepalive_is_throttled_and_never_navigates_healthy_tab(self):
        page, *_ = login_page(sessions.INTERCARS_HOME)
        item = response(sessions.INTERCARS_HOME)
        page.request = SimpleNamespace(get=AsyncMock(return_value=item))
        page.goto, page.reload = AsyncMock(), AsyncMock()
        with patch.object(sessions, 'authenticated', AsyncMock(return_value=True)), \
             patch.object(self.keeper, 'idle', return_value=True):
            first = await self.keeper.check(SimpleNamespace(pages=[page]))
            await self.keeper.check(SimpleNamespace(pages=[page]))
        self.assertEqual(first['Inter Cars']['status'], 'connected')
        self.assertEqual(first['Inter Cars']['keepalive_status'], 'ok')
        self.assertGreater(first['Inter Cars']['keepalive_checked_at'], 0)
        page.request.get.assert_awaited_once_with(sessions.INTERCARS_HOME, timeout=10000)
        item.dispose.assert_awaited_once()
        page.goto.assert_not_awaited()
        page.reload.assert_not_awaited()

    async def test_expired_intercars_response_is_released_before_idle_home_navigation(self):
        page, *_ = login_page(sessions.INTERCARS_HOME)
        events = []
        item = response('https://account.intercars.eu/authenticationendpoint/login.do')
        item.dispose = AsyncMock(side_effect=lambda: events.append('dispose'))
        page.request = SimpleNamespace(get=AsyncMock(return_value=item))
        page.goto = AsyncMock(side_effect=lambda *a, **k: events.append('navigate'))
        with patch.object(sessions, 'authenticated', AsyncMock(return_value=True)), \
             patch.object(sessions, 'upstream_error', AsyncMock(return_value=False)), \
             patch.object(self.keeper, 'idle', return_value=True):
            state = await self.keeper.check(SimpleNamespace(pages=[page]))
        self.assertEqual(events, ['dispose', 'navigate'])
        self.assertEqual(state['Inter Cars']['status'], 'login_required')

    async def test_active_work_prevents_keepalive_and_tab_restoration(self):
        page, *_ = login_page(sessions.INTERCARS_HOME)
        page.request = SimpleNamespace(get=AsyncMock())
        context = SimpleNamespace(pages=[page], new_page=AsyncMock())
        self.keeper.vaults['AutoTotal'].save('fixture', 'fixture-secret', sessions.AUTOTOTAL_HOME)
        with patch.object(sessions, 'authenticated', AsyncMock(return_value=True)), \
             patch.object(self.keeper, 'idle', return_value=False):
            await self.keeper.check(context)
        page.request.get.assert_not_awaited()
        context.new_page.assert_not_awaited()

    async def test_manual_login_grace_protects_every_supplier(self):
        with patch.dict(os.environ, {'ALBATROS_DATA_DIR': self.folder.name}):
            (Path(self.folder.name) / '.browser-console-activity').touch()
            self.assertFalse(self.keeper.idle())
            self.assertFalse(self.keeper.idle(allow_queued=True))

    async def test_busy_work_never_reloads_an_upstream_error_page(self):
        page, *_ = login_page(sessions.INTERCARS_HOME)
        page.reload = AsyncMock()
        with patch.object(sessions, 'authenticated', AsyncMock(return_value=False)), \
             patch.object(sessions, 'upstream_error', AsyncMock(return_value=True)), \
             patch.object(self.keeper, 'idle', return_value=False):
            state = await self.keeper.check(SimpleNamespace(pages=[page]))
        self.assertEqual(state['Inter Cars']['status'], 'upstream_unavailable')
        page.reload.assert_not_awaited()

    async def test_missing_intercars_reopens_same_context_once_and_leaves_captcha_alone(self):
        self.keeper.vaults['Inter Cars'].save('fixture', 'fixture-secret', sessions.INTERCARS_HOME, strict_location=False)
        page, _, _, button = login_page('https://account.intercars.eu/authenticationendpoint/login.do')
        page.goto = AsyncMock()
        context = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=page))
        with patch.object(sessions, 'authenticated', AsyncMock(return_value=False)), \
             patch.object(sessions, 'upstream_error', AsyncMock(return_value=False)), \
             patch.object(sessions, 'active_verification', AsyncMock(return_value=True)), \
             patch.object(self.keeper, 'idle', return_value=True):
            first = await self.keeper.check(context)
            await self.keeper.check(context)
        self.assertTrue(first['Inter Cars']['tab_restored'])
        self.assertEqual(first['Inter Cars']['status'], 'human_verification_required')
        context.new_page.assert_awaited_once()
        page.goto.assert_awaited_once_with(sessions.INTERCARS_HOME, wait_until='domcontentloaded', timeout=15000)
        button.click.assert_not_awaited()
        self.assertEqual(first['Haynes']['status'], 'standby')

    async def test_network_failure_retries_after_one_minute_without_request_storm(self):
        page = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(side_effect=TimeoutError)))
        for now, expected_calls in [(1000, 1), (1059, 1), (1060, 2)]:
            with patch.object(sessions.time, 'monotonic', return_value=now):
                await self.keeper.keep_session(page, 'Inter Cars')
            self.assertEqual(page.request.get.await_count, expected_calls)


class DsmTabContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.page = SimpleNamespace(url='about:blank', goto=AsyncMock(), close=AsyncMock())
        self.context = SimpleNamespace(pages=[], new_page=AsyncMock(return_value=self.page))
        self.vault = SimpleNamespace(path=SimpleNamespace(exists=lambda: True),
            load=Mock(return_value={'landing': 'https://s.servicemanager.q-service.ro/special/home/index'}))
        self.login_vault = SimpleNamespace(path=SimpleNamespace(exists=lambda: True))
        self.keeper = SimpleNamespace(idle=Mock(return_value=True), last_restore={})

    async def test_missing_dsm_uses_saved_trusted_landing_and_throttles_retries(self):
        self.assertTrue(await dsm_keeper.restore_missing_dsm(self.context, self.vault, self.login_vault, self.keeper))
        self.assertFalse(await dsm_keeper.restore_missing_dsm(self.context, self.vault, self.login_vault, self.keeper))
        self.context.new_page.assert_awaited_once()
        self.page.goto.assert_awaited_once_with('https://s.servicemanager.q-service.ro/special/home/index',
            wait_until='domcontentloaded', timeout=15000)

    async def test_existing_login_page_and_active_work_prevent_duplicate_tabs(self):
        self.context.pages = [SimpleNamespace(url='https://apps.dsmpro.ro/login', is_closed=lambda: False)]
        self.assertFalse(await dsm_keeper.restore_missing_dsm(self.context, self.vault, self.login_vault, self.keeper))
        self.context.pages = []
        self.keeper.idle.return_value = False
        self.assertFalse(await dsm_keeper.restore_missing_dsm(self.context, self.vault, self.login_vault, self.keeper))
        self.context.new_page.assert_not_awaited()

    async def test_untrusted_saved_landing_falls_back_to_public_dsm_home(self):
        self.vault.load.return_value = {'landing': 'https://attacker.example/special/home/index'}
        await dsm_keeper.restore_missing_dsm(self.context, self.vault, self.login_vault, self.keeper)
        self.page.goto.assert_awaited_once_with('https://apps.dsmpro.ro/', wait_until='domcontentloaded', timeout=15000)

    async def test_failed_navigation_closes_only_the_new_blank_page(self):
        self.page.goto.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await dsm_keeper.restore_missing_dsm(self.context, self.vault, self.login_vault, self.keeper)
        self.page.close.assert_awaited_once()
        self.assertFalse(await dsm_keeper.restore_missing_dsm(self.context, self.vault, self.login_vault, self.keeper))


if __name__ == '__main__':
    unittest.main()
