from contextlib import closing
from datetime import datetime, timedelta, timezone
import asyncio
import gc
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import app
import database
import brake_scope as scope

PHONE = '40700100019'
CASE = 'whatsapp:scope-test:case:1'
PLATE = 'B119TST'
VIN = 'WVW00000000000009'
WORK = 'Schimb plăcuțe de frână și ulei motor. Kilometraj 130.353 km.'


class ScopeTests(unittest.TestCase):
    def test_question_needed_for_unspecified_pads_but_not_other_work(self):
        for text in ['schimb placute', 'Înlocuire plăcuțe de frână', 'pastile frana',
                     'schimb amortizoare fata si placute de frana', 'placute fata sau spate',
                     'discuri si placute fata sau spate']:
            self.assertTrue(scope.needs_clarification(text), text)
        for text in ['Vopsit bara fata bara spate', 'schimb ulei motor', 'placute fata',
                     'plăcuțe de frână spate', 'plăcuțe de pe față', 'plăcuțe față și spate',
                     'discuri si placute fata', 'placute si discuri spate']:
            self.assertFalse(scope.needs_clarification(text), text)

    def test_other_components_axle_does_not_choose_pads_axle(self):
        for text in ['discuri spate si placute', 'amortizoare fata si placute',
                     'placute si amortizoare spate', 'placute. Discuri fata']:
            self.assertTrue(scope.needs_clarification(text), text)

    def test_reply_requires_an_explicit_choice_not_yes_or_offer_acceptance(self):
        for text, expected in [('Față', 'front'), ('Pe puntea spate', 'rear'), ('ambele punți', 'both'),
                               ('față/spate', 'both'), ('plăcuțele de frână spate', 'rear'),
                               ('plăcuțe pe față și pe spate', 'both'), ('plăcuțele ambele punți', 'both')]:
            self.assertEqual(scope.reply_axle(text), expected)
        for text in ['Da', 'Ok', 'ACCEPT', 'nu stiu', 'probabil fata', 'nu fata ci spate',
                     'amortizoare fata', 'schimb ulei']:
            self.assertIsNone(scope.reply_axle(text), text)

    def test_completed_text_is_usable_by_parts_and_both_time_workers(self):
        from technician_request_worker_v6_time_parse_fix import detect_operations
        for axle, expected in [('front', {'brake_pads_front'}), ('rear', {'brake_pads_rear'}),
                               ('both', {'brake_pads_front', 'brake_pads_rear'})]:
            text = scope.complete(WORK, axle)
            self.assertIn('ulei motor', text); self.assertIn('130.353', text)
            operations = {x['id'] for x in detect_operations(text) if x['id'].startswith('brake_pads_')}
            self.assertEqual(operations, expected)
            kinds = set(app._required_offer_part_kinds(text))
            self.assertEqual(kinds & {'brake_pads', 'brake_pads_rear'},
                             {'brake_pads'} if axle == 'front' else {'brake_pads_rear'} if axle == 'rear'
                             else {'brake_pads', 'brake_pads_rear'})


class PersistedScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'requests.db'
        for mock in [patch.object(database, 'DB_PATH', self.path),
                     patch.object(database, 'known_dsm_vehicle', return_value=None),
                     patch.object(app, 'DB_PATH', self.path), patch.object(app, 'DATA_DIR', self.root),
                     patch.object(app, 'NEW_VEHICLE_STATE_FILE', self.root/'intake.json'),
                     patch.object(app, '_archive_ids', return_value=set())]:
            mock.start(); self.addCleanup(mock.stop)
        self.addCleanup(gc.collect)  # Existing database helpers rely on GC to close SQLite handles on Windows.
        database.init_db()
        self.ident = database.create_request(dict(session_id=CASE, phone=PHONE, registration=PLATE,
                    vin=VIN, vehicle='Test car', symptom=WORK, category='MECANICĂ'))
        self.send = AsyncMock(return_value={'messages': [{'id': 'test-meta-id'}], 'delivery_status': 'accepted_by_meta'})

    def row(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            return dict(conn.execute('SELECT * FROM service_requests WHERE id=?', (self.ident,)).fetchone())

    async def ask(self, send=None, window=True):
        return await scope.ask(self.path, self.ident, send or self.send, lambda _: window)

    async def test_send_claim_precedes_transport_and_retry_is_idempotent(self):
        async def send(phone, message, case):
            self.assertEqual(scope.record(self.path, self.ident)['state'], 'sending')
            self.assertEqual((phone, message, case), (PHONE, scope.question(PLATE), CASE))
            return {'messages': [{'id': 'message'}], 'delivery_status': 'accepted_by_meta'}
        transport = AsyncMock(side_effect=send)
        first = await self.ask(transport)
        self.assertEqual(first['state'], 'submitted')
        self.assertEqual(first['delivery_status'], 'accepted_by_meta')
        self.assertTrue((await self.ask(transport))['already_requested'])
        transport.assert_awaited_once()

    async def test_timeout_restart_and_missing_id_never_resend(self):
        self.send.side_effect = TimeoutError()
        self.assertEqual((await self.ask())['state'], 'uncertain')
        self.send.side_effect = None
        await self.ask()
        self.send.assert_awaited_once()

    async def test_missing_id_is_not_proof_of_send(self):
        self.send.return_value = {}
        self.assertEqual((await self.ask())['state'], 'uncertain')
        await self.ask(); self.send.assert_awaited_once()

    async def test_concurrent_requests_send_only_once(self):
        async def send(*args):
            await asyncio.sleep(.01)
            return {'messages':[{'id':'test-id'}]}
        transport = AsyncMock(side_effect=send)
        results = await asyncio.gather(self.ask(transport), self.ask(transport))
        transport.assert_awaited_once()
        self.assertEqual(sum(bool(x['already_requested']) for x in results), 1)

    async def test_fast_client_reply_is_not_overwritten_by_send_completion(self):
        async def send(*args):
            scope.apply_reply(self.path, self.ident, PHONE, CASE, PLATE, 'rear')
            return {'messages':[{'id':'test-id'}]}
        self.assertEqual((await self.ask(send))['state'], 'answered')
        self.assertEqual(scope.record(self.path, self.ident)['state'], 'answered')

    async def test_explicit_delivery_failure_is_reported_without_resend(self):
        self.send.return_value['delivery_status'] = 'failed'
        self.assertEqual((await self.ask())['state'], 'failed')
        await self.ask(); self.send.assert_awaited_once()

    async def test_closed_whatsapp_window_does_not_claim_or_send(self):
        with self.assertRaisesRegex(ValueError, '24'):
            await self.ask(window=False)
        self.send.assert_not_awaited()
        self.assertIsNone(scope.record(self.path, self.ident))

    async def test_reply_updates_same_request_preserves_vin_other_work_and_clears_old_hours(self):
        await self.ask()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('UPDATE service_requests SET technical_labour_time=9,booking_time=10 WHERE id=?', (self.ident,))
        text = scope.apply_reply(self.path, self.ident, PHONE, CASE, PLATE, 'rear')
        row = self.row()
        self.assertEqual(row['vin'], VIN)
        self.assertEqual(row['symptom'], text); self.assertIn('spate', text)
        self.assertIn('ulei motor', text); self.assertIn('130.353', text)
        self.assertIsNone(row['technical_labour_time']); self.assertIsNone(row['booking_time'])
        self.assertIn('spate', json.loads(row['requested_operations'])[0])
        self.assertEqual(scope.record(self.path, self.ident)['state'], 'answered')
        self.assertEqual(scope.apply_reply(self.path, self.ident, PHONE, CASE, PLATE, 'rear'), text)

    async def test_other_phone_case_or_vehicle_cannot_modify_the_request(self):
        await self.ask()
        for sender, case, plate in [('40700100018', CASE, PLATE), (PHONE, 'other', PLATE), (PHONE, CASE, 'B118TST')]:
            with self.assertRaises(ValueError):
                scope.apply_reply(self.path, self.ident, sender, case, plate, 'front')
        self.assertEqual(self.row()['symptom'], WORK)

    async def test_newer_request_for_same_phone_prevents_wrong_case_send_or_update(self):
        database.create_request(dict(session_id='new-case', phone='+'+PHONE, registration='B118TST', symptom='Schimb ulei'))
        with self.assertRaisesRegex(ValueError, 'mai nouă'):
            await self.ask()
        self.send.assert_not_awaited()
        with self.assertRaises(ValueError):
            scope.apply_reply(self.path, self.ident, PHONE, CASE, PLATE, 'front')

    async def test_expired_reply_is_not_applied(self):
        await self.ask()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('UPDATE brake_scope_messages SET created_at=?', ((datetime.now(timezone.utc)-timedelta(hours=25)).isoformat(),))
        with self.assertRaisesRegex(ValueError, 'expirat'):
            scope.apply_reply(self.path, self.ident, PHONE, CASE, PLATE, 'front')
        self.assertEqual(self.row()['symptom'], WORK)

    async def test_cannot_overwrite_an_axle_confirmed_elsewhere(self):
        await self.ask()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('UPDATE service_requests SET symptom=?', ('Schimb placute fata',))
        with self.assertRaises(ValueError):
            scope.apply_reply(self.path, self.ident, PHONE, CASE, PLATE, 'rear')
        self.assertEqual(self.row()['symptom'], 'Schimb placute fata')

    async def test_app_sender_logs_into_exact_case_and_restarts_preserve_reply_routing(self):
        response = {'messages': [{'id': 'test-meta-id'}]}
        with patch.object(app, '_has_open_whatsapp_window', return_value=True), \
                patch.object(app, 'send_whatsapp_text', AsyncMock(return_value=response)) as send, \
                patch.dict(app.WHATSAPP_DELIVERY_STATUS, {'test-meta-id': {'status': 'delivered'}}), \
                patch.object(app, 'create_request') as create, patch.object(app, '_start_offer_worker') as worker, \
                patch.dict(app._active_case_by_sender, {PHONE:'stale-case'}), patch.dict(app._plate_by_sender, clear=True):
            result = await app._ask_request_brake_scope(self.ident)
            self.assertEqual(result['delivery_status'], 'delivered')
            send.assert_awaited_once_with(PHONE, scope.question(PLATE), case_id=CASE)
            await app._ask_request_brake_scope(self.ident)
            send.assert_awaited_once()
            flow = app._new_vehicle_states()[PHONE]
            self.assertEqual(flow['stage'], 'awaiting_brake_axle')
            self.assertEqual(flow['request_id'], self.ident)
            self.assertEqual(app._get_or_create_case(PHONE), CASE)
            self.assertTrue(await app._route_new_vehicle_intake(PHONE, PLATE+' pe spate'))
            self.assertEqual(self.row()['id'], self.ident)
            self.assertIn('spate', self.row()['symptom'])
            self.assertEqual(app._new_vehicle_states()[PHONE]['stage'], 'request_created')
            self.assertTrue(await app._route_new_vehicle_intake(PHONE, 'spate'))
            create.assert_not_called(); worker.assert_not_called()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0], 1)

    async def test_new_intake_records_pending_request_and_asks_without_starting_offer(self):
        flow = {'stage':'vehicle_identified','case_id':'new-intake','registration':'B118TST', 'vin':VIN,
                'vehicle':'Test car','identification_source':'talon','dsm_transactions':[]}
        app._save_new_vehicle_states({'40700100018': flow})
        with patch.object(app, 'create_request', side_effect=database.create_request), \
                patch.object(app, '_has_open_whatsapp_window', return_value=True), \
                patch.object(app, 'send_whatsapp_text', AsyncMock(return_value={'messages':[{'id':'test-meta-id'}]})) as send, \
                patch.dict(app.WHATSAPP_DELIVERY_STATUS, {'test-meta-id':{'status':'delivered'}}), \
                patch.object(app, '_start_offer_worker') as worker:
            self.assertTrue(await app._route_new_vehicle_intake('40700100018', 'schimb placute'))
            self.assertIn('pe ambele punți', send.await_args.args[1])
            self.assertEqual(app._new_vehicle_states()['40700100018']['stage'], 'awaiting_brake_axle')
            worker.assert_not_called()

    def test_parts_and_labour_cannot_default_to_front_axle(self):
        with self.assertRaises(app.HTTPException) as error:
            app._start_offer_worker(self.ident, self.row())
        self.assertEqual(error.exception.status_code, 409)
        with self.assertRaises(app.HTTPException) as error:
            app._require_advisor_labour_review(self.ident)
        self.assertEqual(error.exception.status_code, 409)
        with patch.object(app.POLICY, 'require_robot'):
            with self.assertRaises(app.HTTPException):
                app.request_technician_start(self.ident)

    async def test_llm_tool_cannot_save_an_incomplete_pad_request(self):
        from agent import AppContext, save_service_request
        from agents.tool_context import ToolContext
        arguments = json.dumps({'symptom':'Schimb placute'})
        context = ToolContext(context=AppContext(session_id=CASE), tool_name='save_service_request',
                              tool_call_id='scope-test', tool_arguments=arguments)
        with patch('agent.create_request') as create:
            result = await save_service_request.on_invoke_tool(context, arguments)
            self.assertIn('CLARIFICARE NECESARĂ', result)
            create.assert_not_called()

    async def test_unauthenticated_admin_endpoint_cannot_send(self):
        from starlette.requests import Request
        request = Request({'type':'http','method':'POST','path':'/','headers':[]})
        with patch.object(app, '_ask_request_brake_scope', AsyncMock()) as ask:
            with self.assertRaises(app.HTTPException) as error:
                await app.master_brake_clarification(self.ident, request)
            self.assertEqual(error.exception.status_code, 403)
            ask.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
