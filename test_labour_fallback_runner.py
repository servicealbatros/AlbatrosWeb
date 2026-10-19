import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import labour_fallback_runner as runner
from worker_run_guard import register_worker_run

class LabourFallbackTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        root=Path(self.folder.name)
        self.haynes=root/'haynes.json'; self.tecrmi=root/'tecrmi.json'
        self.token=register_worker_run([self.haynes,self.tecrmi])
        self.env=patch.dict(os.environ,{'ALBATROS_WORKER_RUN_ID':self.token});self.env.start();self.addCleanup(self.env.stop)
        self.args=('10','TESTVIN','frane',self.haynes,root/'offer.json',self.tecrmi,root/'haynes.log',root/'tecrmi.log','haynes.py','tecrmi.py')

    def test_complete_tecrmi_skips_haynes_entirely(self):
        self.tecrmi.write_text(json.dumps({'status':'ready','technical_hours':1,'detected_operations':[{'id':'pads'}],'resolved_operations':[{'id':'pads'}]}))
        with patch.object(runner,'run_worker',return_value=0) as primary,patch.object(runner,'haynes_on_demand',AsyncMock()) as reserve:
            runner.run(*self.args)
        primary.assert_called_once(); reserve.assert_not_awaited()
        self.assertTrue(json.loads(self.haynes.read_text())['skipped_due_to_tecrmi_complete'])

    def test_missing_operation_requires_reserve(self):
        result={'status':'ready','technical_hours':1,'detected_operations':[{'id':'front'},{'id':'rear'}],'resolved_operations':[{'id':'front'}]}
        self.assertTrue(runner.needs_haynes(result))
        self.tecrmi.write_text(json.dumps(result))
        with patch.object(runner,'run_worker',return_value=0),patch.object(runner,'haynes_on_demand',AsyncMock(return_value=0)) as reserve:
            runner.run(*self.args)
        reserve.assert_awaited_once()

    def test_timeout_ends_primary_running_state_before_reserve(self):
        self.tecrmi.write_text('{"status":"running"}')
        with patch.object(runner,'run_worker',return_value=124),patch.object(runner,'haynes_on_demand',AsyncMock(return_value=124)):
            runner.run(*self.args)
        self.assertEqual(json.loads(self.tecrmi.read_text())['status'],'error')
        self.assertEqual(json.loads(self.haynes.read_text())['status'],'error')

    def test_superseded_request_never_starts_reserve_or_overwrites_results(self):
        self.tecrmi.write_text('{"status":"needs_verification"}')
        register_worker_run([self.haynes,self.tecrmi])
        self.haynes.write_text('{"status":"newer-run"}')
        with patch.object(runner,'run_worker',return_value=0),patch.object(runner,'haynes_on_demand',AsyncMock()) as reserve:
            runner.run(*self.args)
        reserve.assert_not_awaited()
        self.assertEqual(json.loads(self.haynes.read_text())['status'],'newer-run')

    def test_invalid_hours_do_not_skip_reserve(self):
        for hours in (0,-1,'bad',float('nan'),float('inf')):
            self.assertTrue(runner.needs_haynes({'status':'ready','technical_hours':hours}))

class StandbyTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_demand_closes_only_its_own_tab(self):
        from unittest.mock import MagicMock
        for preexisting in (True, False):
            page=SimpleNamespace(url='https://www.workshopdata.com/touch/site/layout/makesOverview',
                is_closed=lambda:False, goto=AsyncMock(), close=AsyncMock(),
                locator=lambda _:SimpleNamespace(count=AsyncMock(return_value=0)))
            context=SimpleNamespace(pages=[page] if preexisting else [],new_page=AsyncMock(return_value=page))
            browser=SimpleNamespace(contexts=[context])
            chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser))
            manager=MagicMock(); manager.__aenter__=AsyncMock(return_value=SimpleNamespace(chromium=chromium));manager.__aexit__=AsyncMock(return_value=False)
            with patch('playwright.async_api.async_playwright',return_value=manager),patch.object(runner,'run_worker',return_value=0):
                self.assertEqual(await runner.haynes_on_demand(['worker'],'test.log',60),0)
            if preexisting:
                page.goto.assert_not_awaited();page.close.assert_not_awaited()
            else:
                page.goto.assert_awaited_once();page.close.assert_awaited_once()

    async def test_haynes_is_not_authenticated_by_background_keeper(self):
        from cryptography.fernet import Fernet
        import provider_sessions as sessions
        with tempfile.TemporaryDirectory() as root:
            keeper=sessions.ProviderSessionKeeper(Path(root)/'profile',Fernet.generate_key().decode())
            keeper.vaults['Haynes'].save('user','test-password','https://www.workshopdata.com/login')
            page=SimpleNamespace(url='https://www.workshopdata.com/login',is_closed=lambda:False)
            with patch.object(sessions,'authenticated',AsyncMock()) as auth,patch.object(sessions,'reconnect',AsyncMock()) as login:
                result=await keeper.check(SimpleNamespace(pages=[page]))
            self.assertEqual(result['Haynes']['status'],'standby')
            auth.assert_not_awaited(); login.assert_not_awaited()

if __name__=='__main__':unittest.main()
