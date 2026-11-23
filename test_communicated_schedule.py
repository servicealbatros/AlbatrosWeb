import ast
from contextlib import closing
from datetime import date, datetime, timezone
import gc
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException

import communicated_schedule as sent
import scheduler
from dsm_schedule_sync import record_mechanic_schedule_delivery, sync_local_schedule


class CommunicatedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.db = Path(self.temp.name) / 'test.db'
        scheduler.init_scheduler_db(self.db)
        self.mihai = scheduler.save_technician(self.db, {'name': 'MIHAI', 'skills': {'diagnoza': 5}})['id']
        self.other = scheduler.save_technician(self.db, {'name': 'Fixture mechanic', 'skills': {'diagnoza': 5, 'frane': 5}})['id']
        self.body = scheduler.save_technician(self.db, {'name': 'Fixture bodyshop', 'department': 'tinichigerie_vopsitorie', 'skills': {'tinichigerie': 5}})['id']
        self.booking = scheduler.create_appointment(self.db, dict(technician_id=self.other,
            start_at='2026-11-18T08:30', end_at='2026-11-18T09:30', registration='B100TST',
            job_number='APP-2184', operations='diagnoza dificila trepidatii'))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE scheduler_appointments SET dsm_appointment_id='APP-2184',source='dsm_schedule_readonly' WHERE id=?", (self.booking['id'],))
            conn.commit()
        self.entries = [dict(id=self.booking['id'], registration='B100TST', dsm_appointment_id='APP-2184',
                             start_at='2026-11-18T08:30', end_at='2026-11-18T09:30')]
        record_mechanic_schedule_delivery(self.db, date(2026, 11, 18), self.mihai, 'fixture', 'fixture-id', 'sent', {})

    def current(self):
        return scheduler.get_appointment(self.db, self.booking['id'])

    def balance(self, department='mecanica', now=None):
        return scheduler.distribute_dsm_appointments_by_capacity(self.db, '2026-11-16', department,
            now=now or datetime(2026, 11, 17, 12))

    def test_exact_recovery_is_idempotent_and_balancer_preserves_communicated_work(self):
        self.assertEqual(sent.restore(self.db, self.mihai, '2026-11-18', self.entries)['restored_ids'], [self.booking['id']])
        before = self.current()
        self.assertEqual(before['technician_id'], self.mihai)
        sent.restore(self.db, self.mihai, '2026-11-18', self.entries)
        result = self.balance()
        self.assertEqual(result['appointment_count'], 0)
        self.assertEqual(self.current()['technician_id'], self.mihai)
        self.assertEqual(self.current()['start_at'], before['start_at'])
        self.assertEqual(self.current()['status'], 'programat')

    def test_sent_dsm_source_and_parent_of_sent_split_are_protected(self):
        sent.record_sent(self.db, self.other, [self.current()])
        result = self.balance()
        self.assertEqual(result['appointment_count'], 0)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DELETE FROM scheduler_communicated_appointments')
            child = conn.execute('''INSERT INTO scheduler_appointments
                (technician_id,start_at,end_at,registration,operations,estimated_hours,source,continued_from_id)
                VALUES (?,'2026-11-18T09:30','2026-11-18T10:30','B100TST','diagnoza dificila',1,'dsm_capacity_split_local',?)''',
                (self.other, self.booking['id'])).lastrowid
            conn.commit()
        sent.record_sent(self.db, self.other, [scheduler.get_appointment(self.db, child)])
        self.assertEqual(self.balance()['appointment_count'], 0)
        self.assertIsNotNone(scheduler.get_appointment(self.db, child))

    def test_rebalancing_another_department_does_not_delete_existing_split(self):
        with closing(sqlite3.connect(self.db)) as conn:
            child = conn.execute('''INSERT INTO scheduler_appointments
                (start_at,end_at,operations,estimated_hours,source,continued_from_id,department)
                VALUES ('2026-11-18T09:30','2026-11-18T10:30','diagnoza',1,'dsm_capacity_split_local',?,'mecanica')''',
                (self.booking['id'],)).lastrowid
            conn.commit()
        self.balance('tinichigerie_vopsitorie')
        self.assertIsNotNone(scheduler.get_appointment(self.db, child))

    def test_import_preserves_sent_and_manual_allocations_but_applies_explicit_dsm_cancellation(self):
        sent.restore(self.db, self.mihai, '2026-11-18', self.entries)
        original = self.current()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE scheduler_appointments SET source='dsm_schedule_readonly' WHERE id=?", (self.booking['id'],))
            conn.commit()
        row = dict(dsm_appointment_id='APP-2184', dsm_native_appointment_id='20391', mechanic='Fixture mechanic', registration='B100TST',
                   start_at='2026-11-18T10:00', end_at='2026-11-18T11:00', operations='diagnoza noua')
        sync_local_schedule(self.db, [row])
        current = self.current()
        self.assertEqual((current['technician_id'], current['start_at'], current['operations']),
                         (self.mihai, original['start_at'], original['operations']))
        self.assertEqual(current['dsm_original_start_at'], '2026-11-18T10:00')
        self.assertEqual(current['dsm_native_appointment_id'], '20391')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE scheduler_appointments SET source='local_schedule_override' WHERE id=?", (self.booking['id'],))
            conn.execute('DELETE FROM scheduler_communicated_appointments')
            conn.commit()
        sync_local_schedule(self.db, [row])
        self.assertEqual(self.current()['technician_id'], self.mihai)
        sync_local_schedule(self.db, [{**row, 'status': 'anulat'}])
        self.assertEqual(self.current()['status'], 'anulat')

    def test_changed_references_missing_delivery_and_conflicts_leave_allocations_untouched(self):
        original = self.current()
        for change in ({'registration': 'B101TST'}, {'dsm_appointment_id': 'APP-2185'}, {'start_at': '2026-11-18T09:00'}):
            with self.assertRaises(ValueError):
                sent.restore(self.db, self.mihai, '2026-11-18', [{**self.entries[0], **change}])
        with self.assertRaises(ValueError):
            sent.restore(self.db, self.mihai, '2026-11-19', self.entries)
        scheduler.create_appointment(self.db, dict(technician_id=self.mihai, start_at='2026-11-18T08:30',
            end_at='2026-11-18T09:30', operations='diagnoza dificila trepidatii'))
        with self.assertRaises(ValueError):
            sent.restore(self.db, self.mihai, '2026-11-18', self.entries)
        self.assertEqual(self.current()['technician_id'], original['technician_id'])

    def test_only_tomorrow_unsent_is_balanced_and_other_days_remain_identical(self):
        ids = {}
        with closing(sqlite3.connect(self.db)) as conn:
            for day in (17, 19, 20):
                ids[day] = conn.execute('''INSERT INTO scheduler_appointments
                    (technician_id,start_at,end_at,operations,estimated_hours,source)
                    VALUES (?,?,?,'diagnoza dificila trepidatii',1,'dsm_schedule_readonly')''',
                    (self.other, f'2026-11-{day}T08:30', f'2026-11-{day}T09:30')).lastrowid
            sent_id = conn.execute('''INSERT INTO scheduler_appointments
                (technician_id,start_at,end_at,operations,estimated_hours,source)
                VALUES (?,'2026-11-19T10:30','2026-11-19T11:30','diagnoza dificila trepidatii',1,'dsm_schedule_readonly')''',
                (self.mihai,)).lastrowid
            conn.commit()
        sent.record_sent(self.db, self.mihai, [scheduler.get_appointment(self.db, sent_id)])
        fixed = [self.booking['id'], ids[17], ids[20], sent_id]
        before = {ident: scheduler.get_appointment(self.db, ident) for ident in fixed}
        # Even an old calendar page cannot rebalance another day or week.
        result = scheduler.distribute_dsm_appointments_by_capacity(self.db, '2026-01-01', 'all', now=datetime(2026,11,18,12))
        self.assertEqual(result['date'], '2026-11-19')
        self.assertEqual(result['appointment_count'], 1)
        self.assertEqual({r['appointment_id'] for r in result['decisions']}, {ids[19]})
        self.assertEqual(result['communicated_preserved'], 1)
        for ident in fixed:
            self.assertEqual(scheduler.get_appointment(self.db, ident), before[ident])

    def test_old_delivery_receipts_also_freeze_next_days_assignment(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('UPDATE scheduler_appointments SET technician_id=? WHERE id=?', (self.mihai,self.booking['id']))
            conn.commit()
        self.assertEqual(self.balance()['appointment_count'], 0)
        self.assertEqual(self.balance()['communicated_preserved'], 1)
        sync_local_schedule(self.db, [dict(dsm_appointment_id='APP-2184',mechanic='Fixture mechanic',
            registration='B100TST',start_at='2026-11-18T10:30',end_at='2026-11-18T11:30',operations='diagnoza')])
        self.assertEqual(self.current()['technician_id'], self.mihai)
        self.assertEqual(self.current()['start_at'], '2026-11-18T08:30')

    def test_romanian_midnight_and_week_boundary_use_calendar_tomorrow(self):
        for now, expected in ((datetime(2026,11,17,22,30,tzinfo=timezone.utc),'2026-11-19'),
                              (datetime(2026,7,17,21,30,tzinfo=timezone.utc),'2026-07-19'),
                              (datetime(2026,11,22,12),'2026-11-23')):
            with self.subTest(now=now):
                self.assertEqual(self.balance(now=now)['date'], expected)

    def test_legacy_rebalance_entry_point_cannot_modify_today(self):
        before = self.current()
        result = scheduler.rebalance_schedule(self.db, '2026-11-16', now=datetime(2026,11,18,12))
        self.assertEqual(result['date'], '2026-11-19')
        self.assertEqual(result['appointment_count'], 0)
        self.assertEqual(self.current(), before)


class RecoveryEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_master_and_explicit_action_required_no_message_send(self):
        tree = ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8-sig'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'restore_communicated_schedule')
        node.decorator_list = []
        env = dict(Request=object, HTTPException=HTTPException, _master_access=Mock(), DB_PATH='fixture')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'app.py', 'exec'), env)
        request = SimpleNamespace(headers={}, json=AsyncMock(return_value={'technician_id': 12, 'date': '2026-11-18', 'entries': []}))
        with patch.object(sent, 'restore', Mock(return_value={'ok': True})) as restore:
            with self.assertRaises(HTTPException):
                await env['restore_communicated_schedule'](request)
            restore.assert_not_called()
            request.headers['X-Human-Action'] = 'restore-communicated-schedule'
            await env['restore_communicated_schedule'](request)
            restore.assert_called_once_with('fixture', 12, '2026-11-18', [])
            self.assertEqual(env['_master_access'].call_count, 2)


if __name__ == '__main__':
    unittest.main()
