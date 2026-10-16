import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app
import scheduler


class NotificationDetailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'test.db'
        scheduler.init_scheduler_db(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE service_requests (id INTEGER PRIMARY KEY)')
            conn.execute('INSERT INTO service_requests VALUES (42)')
        conn.close()
        self.booking = scheduler.create_appointment(self.db, {'start_at':'2026-10-19T09:30','end_at':'2026-10-19T11:00','registration':'B105TST','job_number':'APP-123','operations':'Schimb placute'})
        self.patch = patch.object(app, 'DB_PATH', self.db)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        gc.collect()
        self.temp.cleanup()

    def notification(self, request_id, appointment_id=None):
        with sqlite3.connect(self.db) as conn:
            conn.execute('INSERT INTO scheduler_notifications (request_id,appointment_id,message) VALUES (?,?,?)', (request_id, appointment_id or self.booking['id'], 'Intarziere'))
        conn.close()
        return app._scheduler_notifications_for_dashboard()[0]

    def test_dsm_notification_opens_booking_without_fake_request(self):
        result = self.notification(None)
        self.assertFalse(result['request_exists'])
        self.assertTrue(result['appointment_exists'])
        # The booking is outside the currently displayed week, but still opens by ID.
        details = app.scheduler_appointment_details(result['appointment_id'])['appointment']
        self.assertEqual(details['registration'], 'B105TST')
        self.assertEqual(details['operations'], 'Schimb placute')
        self.assertEqual(details['start_at'], '2026-10-19T09:30')

    def test_real_request_keeps_conversation_link(self):
        self.assertTrue(self.notification(42)['request_exists'])

    def test_deleted_request_falls_back_to_booking(self):
        result = self.notification(999)
        self.assertFalse(result['request_exists'])
        self.assertTrue(result['appointment_exists'])

    def test_missing_booking_has_clear_unavailable_state(self):
        result = self.notification(None, 999)
        self.assertFalse(result['appointment_exists'])
        with self.assertRaises(app.HTTPException) as caught:
            app.scheduler_appointment_details(999)
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == '__main__':
    unittest.main()
