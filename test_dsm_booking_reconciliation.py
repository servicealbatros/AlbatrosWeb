import asyncio
import ast
from copy import deepcopy
import gc
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from unittest.mock import Mock
from types import SimpleNamespace
from fastapi import HTTPException

import database
import dsm_booking_reconciliation as sync
from dsm_accepted_offers import apply_snapshot, connection
from scheduler import init_scheduler_db, accepted_requests
from test_dsm_accepted_offers import item


class BookingReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.search = patch.object(sync, 'search_appointments', AsyncMock(return_value=set()))
        self.search.start()
        self.addCleanup(self.search.stop)
        self.db = Path(self.temp.name) / 'test.db'
        with patch.object(database, 'DB_PATH', self.db):
            database.init_db()
        init_scheduler_db(self.db)
        self.ident = apply_snapshot(self.db, [item()])['request_ids'][0]
        self.root = dict(transaction=dict(transaction_head__id='1', transaction_head__type='1',
            transaction_head__code='OFERTA-1', transaction_head__status='working',
            car_body_series='WBA00000000000002', car_auto_number='B100TST', car_id='25'),
            sms_or_qrcode_sended={'service_offer': 'signed'}, transaction_head_childs=[])
        self.detail = dict(code='APP-1952', carId='25', carNumber='B100 TST',
            description='OFERTA 1-PLACUTE', start='2026-10-01T08:30', end='2026-10-01T10:30',
            canCancel=True)
        with connection(self.db) as conn:
            conn.execute('''INSERT INTO scheduler_appointments
                (registration,start_at,end_at,job_number,operations,source,dsm_appointment_id,estimated_hours)
                VALUES ('B100TST','2026-10-01T08:30','2026-10-01T10:30','APP-1952',
                        'OFERTA 1-PLACUTE','dsm_schedule_readonly','APP-1952',2)''')

    async def run_sync(self, apply=True):
        with patch.object(sync, 'read_offer_details', AsyncMock(return_value={'1': deepcopy(self.root)})), \
             patch.object(sync, 'read_appointment', AsyncMock(return_value=deepcopy(self.detail))):
            return await sync.reconcile(self.db, apply=apply)

    def status(self):
        with connection(self.db) as conn:
            return conn.execute('SELECT status FROM service_requests WHERE id=?', (self.ident,)).fetchone()[0]

    async def test_past_booking_resolves_dashboard_without_finishing_calendar_or_deleting_offer(self):
        preview = await self.run_sync(False)
        self.assertEqual(preview['verified'][0]['request_id'], self.ident)
        self.assertEqual(self.status(), 'acceptat')
        result = await self.run_sync()
        self.assertEqual(result['resolved_request_ids'], [self.ident])
        self.assertEqual(self.status(), 'rezolvat')
        self.assertEqual(accepted_requests(self.db), [])
        with connection(self.db) as conn:
            self.assertEqual(conn.execute('SELECT status,request_id FROM scheduler_appointments').fetchone(), ('programat', None))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM dsm_accepted_offer_links').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM dsm_booking_reconciliations').fetchone()[0], 1)
        self.assertEqual((await self.run_sync())['resolved_request_ids'], [])
        apply_snapshot(self.db, [item()])
        self.assertEqual(self.status(), 'rezolvat')

    async def test_future_booking_also_resolves_scheduling_task(self):
        self.detail.update(start='2036-11-20T08:30', end='2036-11-20T10:30')
        self.assertEqual((await self.run_sync())['resolved_request_ids'], [self.ident])

    async def test_live_native_relationship_needs_no_local_calendar_copy(self):
        with connection(self.db) as conn:
            conn.execute('DELETE FROM scheduler_appointments')
        self.root['transaction_head_childs'] = [dict(transaction_id='1952', transaction_head__type='3', transaction_status='0')]
        self.detail['description'] = 'Plăcuțe'
        self.assertEqual((await self.run_sync())['resolved_request_ids'], [self.ident])

    async def test_history_discovers_booking_outside_imported_calendar(self):
        with connection(self.db) as conn:
            conn.execute('DELETE FROM scheduler_appointments')
        sync.search_appointments.return_value = {'1952'}
        self.assertEqual((await self.run_sync())['resolved_request_ids'], [self.ident])

    async def test_plate_alone_stale_description_wrong_identity_and_cancelled_are_preserved(self):
        changes = [dict(carId='26'), dict(carNumber='B101TST'), dict(code='APP-1953'),
                   dict(description='OFERTA 10-ALTA LUCRARE'), dict(description='OFERTA 1 SI OFERTA 2'),
                   dict(status='cancelled'), dict(status='4'), dict(isCanceled=True),
                   dict(canCancel=False), dict(end='2026-10-01T08:00'), dict(start='')]
        original = deepcopy(self.detail)
        for change in changes:
            with self.subTest(change=change):
                self.detail = {**original, **change}
                self.assertEqual((await self.run_sync())['resolved_request_ids'], [])
                self.assertEqual(self.status(), 'acceptat')
        self.detail = original
        for field, value in [('car_body_series', 'WV100000000000008'), ('car_auto_number', 'B101TST'),
                             ('transaction_head__code', 'OFERTA-10'), ('transaction_head__status', 'canceled')]:
            old = self.root['transaction'][field]
            self.root['transaction'][field] = value
            self.assertEqual((await self.run_sync())['resolved_request_ids'], [])
            self.root['transaction'][field] = old
        self.root['sms_or_qrcode_sended'] = {'service_offer': 'sent'}
        self.assertEqual((await self.run_sync())['resolved_request_ids'], [])

    async def test_manual_duplicate_claimed_and_ambiguous_calendar_are_not_resolved(self):
        with connection(self.db) as conn:
            conn.execute("UPDATE scheduler_appointments SET source='manual'")
        self.assertEqual((await self.run_sync())['resolved_request_ids'], [])
        with connection(self.db) as conn:
            conn.execute("UPDATE scheduler_appointments SET source='dsm_schedule_readonly',request_id=999")
        self.root['transaction_head_childs'] = [dict(transaction_id='1952', transaction_head__type='3', transaction_status='0')]
        self.assertEqual((await self.run_sync())['resolved_request_ids'], [])
        self.root['transaction_head_childs'] = []
        with connection(self.db) as conn:
            conn.execute('UPDATE scheduler_appointments SET request_id=NULL')
            conn.execute('''INSERT INTO scheduler_appointments
                (registration,start_at,end_at,operations,source,dsm_appointment_id,estimated_hours)
                VALUES ('B100TST','2026-10-02T08:30','2026-10-02T10:30','OFERTA 1',
                        'dsm_schedule_readonly','APP-1953',2)''')
        async def read(ident):
            return {**self.detail, 'code': 'APP-' + ident}
        with patch.object(sync, 'read_offer_details', AsyncMock(return_value={'1': self.root})), \
             patch.object(sync, 'read_appointment', AsyncMock(side_effect=read)):
            result = await sync.reconcile(self.db, apply=True)
        self.assertEqual(result['resolved_request_ids'], [])
        self.assertEqual(result['pending'][0]['reason'], 'ambiguous_bookings')

    async def test_failed_read_and_concurrent_edit_leave_dashboard_untouched(self):
        with patch.object(sync, 'read_offer_details', AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(TimeoutError):
                await sync.reconcile(self.db, apply=True)
        async def changed(_):
            with connection(self.db) as conn:
                conn.execute("UPDATE service_requests SET status='anulat'")
            return self.detail
        with patch.object(sync, 'read_offer_details', AsyncMock(return_value={'1': self.root})), \
             patch.object(sync, 'read_appointment', AsyncMock(side_effect=changed)):
            self.assertEqual((await sync.reconcile(self.db, apply=True))['resolved_request_ids'], [])
        self.assertEqual(self.status(), 'anulat')

    async def test_archived_requests_and_concurrent_calendar_claim_are_preserved(self):
        async def claimed(_):
            with connection(self.db) as conn:
                conn.execute('UPDATE scheduler_appointments SET request_id=999')
            return self.detail
        with patch.object(sync, 'read_offer_details', AsyncMock(return_value={'1': self.root})), \
             patch.object(sync, 'read_appointment', AsyncMock(side_effect=claimed)):
            self.assertEqual((await sync.reconcile(self.db, apply=True))['resolved_request_ids'], [])
        self.assertEqual(self.status(), 'acceptat')
        with connection(self.db) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS request_archive (request_id INTEGER PRIMARY KEY)')
            conn.execute('INSERT INTO request_archive(request_id) VALUES (?)', (self.ident,))
        with patch.object(sync, 'read_offer_details', AsyncMock()) as read:
            self.assertEqual((await sync.reconcile(self.db, apply=True))['checked'], 0)
            read.assert_not_called()

    async def test_periodic_check_is_bounded_and_exposes_actual_result(self):
        with patch.object(sync, 'reconcile', AsyncMock(side_effect=TimeoutError)) as check:
            first = await sync.tick(self.db, self.temp.name)
            self.assertEqual(first['status'], 'error')
            self.assertEqual(first['error_type'], 'TimeoutError')
            self.assertTrue(first['checked_at'])
            self.assertEqual(await sync.tick(self.db, self.temp.name), first)
            check.assert_awaited_once_with(self.db, apply=True)
        async with sync.LOCK:
            self.assertEqual((await sync.reconcile(self.db, apply=True))['status'], 'running')

    async def test_readers_reject_partial_data_and_only_read_existing_appointment(self):
        for result in ([], [dict(transaction={'transaction_head__id': '2'})]):
            with patch('dsm_open_orders_readonly._evaluate', AsyncMock(return_value=result)):
                with self.assertRaises(ValueError):
                    await sync.read_offer_details(['1'])
        with patch('dsm_pre_arrival.appointment_call', AsyncMock(return_value={'status': 'success', 'detail': self.detail})) as read:
            self.assertEqual(await sync.read_appointment('1952'), self.detail)
            read.assert_awaited_once_with('getappointment', {'appointment_id': '1952'})

    async def test_periodic_reconciliation_runs_once_every_two_hours(self):
        now = datetime(2026, 11, 18, 8, tzinfo=timezone.utc)
        path = Path(self.temp.name) / 'dsm_booking_reconciliation.json'
        path.write_text(json.dumps({'checked_at': (now-timedelta(hours=2)+timedelta(seconds=1)).isoformat()}), encoding='utf-8')
        with patch.object(sync, 'datetime', wraps=datetime) as clock, \
             patch.object(sync, 'reconcile', AsyncMock(return_value={'ok': True})) as check:
            clock.now.return_value = now
            await sync.tick(self.db, self.temp.name)
            check.assert_not_called()
            clock.now.return_value = now + timedelta(seconds=1)
            await sync.tick(self.db, self.temp.name)
            check.assert_awaited_once_with(self.db, apply=True)

    async def test_master_endpoint_requires_explicit_apply_and_redacts_failures(self):
        tree = ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8-sig'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'reconcile_dsm_bookings')
        node.decorator_list = []
        env = dict(Request=object, HTTPException=HTTPException, _master_access=Mock(), DB_PATH=self.db,
                   REQUEST_STATUS={}, save_request_status=Mock())
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'app.py', 'exec'), env)
        request = SimpleNamespace(json=AsyncMock(return_value={'apply': True}), headers={})
        with patch.object(sync, 'reconcile', AsyncMock()) as check:
            with self.assertRaises(HTTPException) as caught:
                await env['reconcile_dsm_bookings'](request)
            self.assertEqual(caught.exception.status_code, 403)
            check.assert_not_called()
        request.headers['X-Human-Action'] = 'reconcile-dsm-bookings'
        with patch.object(sync, 'reconcile', AsyncMock(return_value={'resolved_request_ids': [self.ident]})):
            await env['reconcile_dsm_bookings'](request)
        self.assertEqual(env['REQUEST_STATUS'][self.ident], 'rezolvat')
        env['save_request_status'].assert_called_once()
        with patch.object(sync, 'reconcile', AsyncMock(side_effect=ValueError('private DSM data'))):
            with self.assertRaises(HTTPException) as caught:
                await env['reconcile_dsm_bookings'](request)
            self.assertEqual(caught.exception.status_code, 502)
            self.assertNotIn('private', caught.exception.detail)


if __name__ == '__main__':
    unittest.main()
