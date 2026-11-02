import asyncio
from contextlib import ExitStack
import json
import io
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import app
import autototal_request_worker as auto
import labour_fallback_runner as runner
from labour_vehicle import identity_path, matching, read_identity
import technician_request_worker_v6_time_parse_fix as tech
import tecrmi_request_worker as tecrmi
from worker_run_guard import register_worker_run

VIN = 'WVW00000000000009'
QUERY = 'Schimb plăcuțe de frână față și spate'


class IdentificationTests(unittest.IsolatedAsyncioTestCase):
    async def identify(self, types):
        with TemporaryDirectory() as folder:
            output = Path(folder)/'identity.json'
            decoded = {'td_catalog':{'types':types}, 'link':'https://www3.eoriginal.ro/auto/vin/test',
                       'lx_catalog':{}}
            async def evaluate(ws, ident, expression, *args):
                if 'search/searchVin' in expression:
                    return json.dumps(decoded)
                if expression == 'location.href':
                    return decoded['link']
                return ''
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=object())
            context.__aexit__ = AsyncMock(return_value=False)
            with patch.dict(os.environ, {'ALBATROS_WORKER_RUN_ID':''}), \
                 patch.object(auto, '_autototal_target', return_value={'webSocketDebuggerUrl':'ws://test'}), \
                 patch.object(auto.websockets, 'connect', return_value=context), \
                 patch.object(auto, '_evaluate', AsyncMock(side_effect=evaluate)), \
                 patch.object(auto, '_wait_value', AsyncMock(return_value='\nSKODA SUPERB')), \
                 patch.object(auto, '_load_vehicle_hints', return_value={}), \
                 patch.object(auto, '_standard_vin_types', AsyncMock(return_value=[])), \
                 patch.object(auto.asyncio, 'sleep', AsyncMock()), \
                 patch.object(auto, 'ensure_autototal_responsive', AsyncMock()) as recovery, \
                 patch.object(auto, '_discover_oe_vin_candidates', AsyncMock()) as oe, \
                 patch.object(auto, '_standard_categories', AsyncMock()) as parts:
                await auto.run(10, VIN, QUERY, output, identification_only=True)
                recovery.assert_not_awaited(); oe.assert_not_awaited(); parts.assert_not_awaited()
            return json.loads(output.read_text(encoding='utf-8'))

    async def test_identity_only_selects_type_without_parts_or_tab_recovery(self):
        result = await self.identify([{'typeIdField':'123','typeNameField':'Test'}])
        self.assertEqual(result['status'], 'vehicle_ready')
        self.assertEqual(result['vehicle']['autototal_type_id'], '123')
        self.assertEqual(result['vin'], VIN); self.assertEqual(result['products'], [])

    async def test_missing_types_do_not_fall_back_to_parts_search(self):
        self.assertEqual((await self.identify([]))['status'], 'needs_vehicle_data')

    async def test_ambiguous_type_is_not_guessed(self):
        result = await self.identify([{'typeIdField':'1'},{'typeIdField':'2'}])
        self.assertEqual(result['status'], 'needs_vehicle_data')
        self.assertNotIn('autototal_type_id', result['vehicle'])

    async def test_missing_type_identifier_is_not_published_as_the_string_none(self):
        result = await self.identify([{'typeNameField':'Test'}])
        self.assertEqual(result['status'], 'needs_vehicle_data')

    async def test_standalone_initializes_itself_with_no_parts_result(self):
        with TemporaryDirectory() as folder:
            output = Path(folder)/'labour.json'
            async def identify(request_id, vin, text, target, hints, **kwargs):
                self.assertTrue(kwargs['identification_only'])
                target.write_text(json.dumps({'request_id':10,'vin':VIN,'vehicle':{'autototal_type_id':'12'}}))
            with patch.object(auto, 'run', AsyncMock(side_effect=identify)) as task:
                result = await tecrmi.standalone_identity(10, VIN, QUERY, output, str(Path(folder)/'request_10_offer.json'))
            task.assert_awaited_once()
            self.assertEqual(result['vehicle']['autototal_type_id'], '12')
            self.assertFalse((Path(folder)/'request_10_autototal.json').exists())

    async def test_real_tecrmi_reader_uses_identified_type_and_no_unrequested_oil_call(self):
        with TemporaryDirectory() as folder:
            identity = Path(folder)/'identity.json'
            identity.write_text(json.dumps({'request_id':10,'vin':VIN,'vehicle':{'autototal_type_id':'123'}}))
            leaves = [{'text':f'placute de frana {side} inlocuire','mainGroupIdField':2,
                       'subGroupIdField':3,'itemMpIdField':i,'korIdField':i+10}
                      for i,side in ((1,'fata'),(2,'spate'))]
            expressions = []
            async def cdp(ws, ident, method, params):
                expression = params['expression']; expressions.append(expression)
                self.assertIn('fetch(url', expression)
                self.assertIn('123', expression)
                if '/car/repair/getRepairTime' in expression:
                    side = 'fata' if 'iID:1' in expression else 'spate'
                    value = [{'workTimeField':.5,'full_text':f'placute de frana {side} inlocuire'}]
                else:
                    value = leaves
                return {'result':{'result':{'value':json.dumps(value)}}}
            context = MagicMock(); context.__aenter__=AsyncMock(return_value=object()); context.__aexit__=AsyncMock(return_value=False)
            targets = io.StringIO(json.dumps([{'type':'page','url':'https://www3.eoriginal.ro','webSocketDebuggerUrl':'ws://test'}]))
            with patch.object(tech,'urlopen',return_value=targets), patch.object(tech.websockets,'connect',return_value=context), \
                 patch.object(tech,'_cdp_call',AsyncMock(side_effect=cdp)):
                result = await tech._autototal_repair_times(10,str(Path(folder)/'parts.json'),tech.detect_operations(QUERY),
                                                           identity_file=identity,expected_vin=VIN)
            self.assertEqual({x['id'] for x in result['resolved']},{'brake_pads_front','brake_pads_rear'})
            self.assertEqual(result['unresolved'],[])
            self.assertEqual(len(expressions),3)
            self.assertFalse(any('capacitati' in expr for expr in expressions))


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.output = self.root/'labour.json'
        self.parts = self.root/'request_10_offer.json'
        self.environment = patch.dict(os.environ, {'ALBATROS_WORKER_RUN_ID':''})
        self.environment.start(); self.addCleanup(self.environment.stop)

    def test_wrong_request_or_vin_never_proves_identity(self):
        for payload in ({'request_id':11,'vin':VIN}, {'request_id':10,'vin':'WRONG'}, {'request_id':10}):
            self.assertFalse(matching(payload, 10, VIN))
        self.assertTrue(matching({'request_id':10,'vin':VIN.lower()}, 10, VIN))

    def test_wrong_identity_file_is_rejected_before_catalogue_calls(self):
        self.output.write_text(json.dumps({'request_id':11,'vin':VIN,'vehicle':{'autototal_type_id':'1'}}))
        with patch.object(tech, 'urlopen') as network:
            result = asyncio.run(tech._autototal_repair_times(10, str(self.parts), [], identity_file=self.output, expected_vin=VIN))
        self.assertIsNone(result); network.assert_not_called()

    def test_coupled_wait_rejects_another_request_instead_of_polling_it(self):
        target = self.root/'request_10_autototal.json'
        target.write_text(json.dumps({'request_id':11,'vin':VIN,'vehicle':{'autototal_type_id':'1'}}))
        with patch.object(tech.time, 'sleep') as sleep:
            self.assertFalse(tech._wait_for_autototal_identification(10, str(self.parts), expected_vin=VIN))
        sleep.assert_not_called()

    def test_tecrmi_standalone_never_waits_for_parts_and_reads_both_axles(self):
        operations = tech.detect_operations(QUERY)
        result = {'resolved':[{**op,'technical_hours':.5,'haynes_operation':op['canonical']} for op in operations],
                  'unresolved':[]}
        argv = ['worker','10',VIN,QUERY,str(self.output),str(self.parts),'--standalone']
        snapshots = []
        original_save = tecrmi.save
        def save(path, value):
            snapshots.append(value.copy())
            original_save(path, value)
        with patch.object(tecrmi.sys, 'argv', argv), \
             patch.object(tecrmi, 'save', side_effect=save), \
             patch.object(tecrmi, 'standalone_identity', AsyncMock(return_value={'vehicle':{'autototal_type_id':'12'}})), \
             patch.object(tecrmi, '_wait_for_autototal_identification') as wait, \
             patch.object(tecrmi, '_autototal_repair_times', AsyncMock(return_value=result)) as search:
            tecrmi.main()
        wait.assert_not_called(); search.assert_awaited_once()
        saved = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'ready')
        self.assertEqual(saved['technical_hours'], 1)
        self.assertEqual(saved['vin'], VIN)
        self.assertTrue(next(x for x in snapshots if x['status']=='running')['started_at'])
        self.assertEqual({op['id'] for op in saved['resolved_operations']}, {'brake_pads_front','brake_pads_rear'})

    def test_identification_timeout_preserves_identity_and_does_not_read_arbitrary_type(self):
        argv = ['worker','10',VIN,QUERY,str(self.output),str(self.parts),'--standalone']
        with patch.object(tecrmi.sys, 'argv', argv), \
             patch.object(tecrmi, 'standalone_identity', AsyncMock(side_effect=TimeoutError())), \
             patch.object(tecrmi, '_autototal_repair_times', AsyncMock()) as search:
            tecrmi.main()
        search.assert_not_awaited()
        saved = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertTrue(matching(saved, 10, VIN)); self.assertEqual(saved['status'], 'error')

    def test_haynes_does_not_wait_or_read_previously_selected_car(self):
        page = SimpleNamespace(url='https://www.workshopdata.com/repairTimes',
                               locator=lambda _:SimpleNamespace(inner_text=lambda **_: 'Different vehicle'))
        manager = MagicMock(); browser = object()
        manager.__enter__.return_value = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *a,**k:browser))
        argv = ['worker','10',VIN,QUERY,str(self.output),str(self.parts),'--standalone']
        with patch.object(tech.sys, 'argv', argv), patch.object(tech, 'sync_playwright', return_value=manager), \
             patch.object(tech, 'find_haynes_page', return_value=page), \
             patch.object(tech, 'wait_for_offer_products') as wait, \
             patch.object(tech, '_haynes_drivetrain_conflict', AsyncMock()) as navigate, \
             patch.object(tech, 'ensure_autototal_responsive', AsyncMock()) as recover, \
             patch.object(tech, 'haynes_text_snapshot') as snapshot:
            tech.main()
        wait.assert_not_called(); navigate.assert_not_awaited(); recover.assert_not_awaited(); snapshot.assert_not_called()
        self.assertEqual(json.loads(self.output.read_text(encoding='utf-8'))['status'], 'needs_vehicle_data')

    def test_standalone_flag_reaches_both_workers_and_timeouts_keep_vin(self):
        haynes = self.root/'haynes.json'
        args = ('10', VIN, QUERY, haynes, self.parts, self.output, self.root/'h.log', self.root/'t.log', 'h.py','t.py','standalone')
        self.output.write_text(json.dumps({'request_id':10,'vin':VIN,'status':'running'}))
        token = register_worker_run([haynes,self.output])
        with patch.dict(os.environ, {'ALBATROS_WORKER_RUN_ID':token}), \
             patch.object(runner,'run_worker',return_value=124) as primary, \
             patch.object(runner,'haynes_on_demand',AsyncMock(return_value=124)) as reserve:
            runner.run(*args)
        self.assertEqual(primary.call_args.args[0][-1], '--standalone')
        self.assertEqual(reserve.call_args.args[0][-1], '--standalone')
        for path in (haynes,self.output):
            self.assertTrue(matching(json.loads(path.read_text(encoding='utf-8')),10,VIN))

    def test_generic_haynes_overview_is_not_vehicle_confirmation(self):
        page = SimpleNamespace(url='https://www.workshopdata.com/makesOverview',
                               locator=lambda _:SimpleNamespace(inner_text=lambda **_:VIN))
        self.assertFalse(tech.haynes_matches_vin(page,VIN))

    def test_labour_only_route_explicitly_requests_standalone_mode(self):
        with patch.object(app,'_request_by_id',return_value={'id':10}), \
             patch.object(app,'request_technician_start',return_value={'ok':True}) as start:
            self.assertFalse(app.request_labour_only_start(10)['parts_search_started'])
        start.assert_called_once_with(10, standalone=True)

    def test_real_start_publishes_identity_inputs_and_preserves_parts_files(self):
        for standalone in (True, False):
            with self.subTest(standalone=standalone), ExitStack() as stack:
                for name in ('TECHNICIAN_OUTPUT_DIR','TECRMI_OUTPUT_DIR','TECHNICIAN_LOG_DIR','TECRMI_LOG_DIR'):
                    folder = self.root/name; folder.mkdir(exist_ok=True)
                    stack.enter_context(patch.object(app,name,folder))
                stack.enter_context(patch.object(app,'TECHNICIAN_PYTHON',Path(sys.executable)))
                stack.enter_context(patch.object(app,'_request_by_id',return_value={'id':10,'vin':VIN,'symptom':QUERY}))
                stack.enter_context(patch.object(app,'_dsm_history_labour_fallback',return_value=None))
                stack.enter_context(patch.object(app,'_vehicle_hints_for_request',return_value={'make':'Test'}))
                stack.enter_context(patch.object(app.POLICY,'require_robot'))
                stack.enter_context(patch.object(app,'_load_robot_jobs',return_value=[] if standalone else [{'request_id':10,'status':'RUNNING'}]))
                start = stack.enter_context(patch.object(app.subprocess,'Popen'))
                self.parts.write_text('original parts result')
                result = app.request_technician_start(10)
                self.assertTrue(result['ok'])
                self.assertEqual(start.call_args.args[0][-1], 'standalone' if standalone else 'coupled')
                self.assertEqual(self.parts.read_text(), 'original parts result')
                identity = identity_path(app._tecrmi_result_file(10))
                from worker_run_guard import write_worker_result
                self.assertTrue(write_worker_result(identity, {'test':True}, start.call_args.kwargs['env']['ALBATROS_WORKER_RUN_ID']))

    def test_standalone_does_not_navigate_over_another_active_parts_job(self):
        with patch.object(app,'_request_by_id',return_value={'id':10,'vin':VIN,'symptom':QUERY}), \
             patch.object(app.POLICY,'require_robot'), \
             patch.object(app,'_load_robot_jobs',return_value=[{'request_id':11,'status':'RUNNING'}]), \
             patch.object(app.subprocess,'Popen') as start:
            with self.assertRaises(app.HTTPException) as exc:
                app.request_technician_start(10, standalone=True)
        self.assertEqual(exc.exception.status_code,409); start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
