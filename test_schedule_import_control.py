import tempfile
import unittest
from datetime import datetime, timedelta
from schedule_import_control import import_due, finish_import, write_state, read_state

class PeriodicImportTests(unittest.TestCase):
    def test_success_waits_five_hours_and_survives_restart(self):
        now = datetime(2026, 10, 14, 9)
        state = {'enabled': True, 'interval_hours': 5}
        self.assertTrue(import_due(state, now))
        finish_import(state, {'ok': True, 'days_imported': 8}, now)
        with tempfile.TemporaryDirectory() as root:
            write_state(root, state)
            restored = read_state(root)
        self.assertFalse(import_due(restored, now + timedelta(hours=4, minutes=59)))
        self.assertTrue(import_due(restored, now + timedelta(hours=5)))

    def test_partial_does_not_claim_success_and_retries_after_fifteen_minutes(self):
        now = datetime(2026, 10, 14, 9)
        state = {'enabled': True, 'interval_hours': 5, 'last_success': '2026-10-13T09:00:00'}
        finish_import(state, {'ok': False, 'days_imported': 2, 'errors':[{'error':'authentication required'}]}, now)
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(state['last_success'], '2026-10-13T09:00:00')
        self.assertFalse(import_due(state, now + timedelta(minutes=14)))
        self.assertTrue(import_due(state, now + timedelta(minutes=15)))
        state['enabled'] = False
        self.assertFalse(import_due(state, now + timedelta(days=1)))
