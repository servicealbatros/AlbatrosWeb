import gc
import sqlite3
import tempfile
import unittest
import warnings
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
import scheduler


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name)/'scheduler.db'
        scheduler.init_scheduler_db(self.db)
        self.owner = scheduler.save_technician(self.db, {'name':'ADRIAN','skills':{'frane':5}})['id']
        self.reporter = scheduler.save_technician(self.db, {'name':'ADELIN','skills':{'frane':5}})['id']
        self.booking = scheduler.create_appointment(self.db, {'technician_id':self.owner,'start_at':'2026-10-16T08:30','end_at':'2026-10-16T11:00','registration':'B104TST','job_number':'APP-1887','operations':'placute fata spate','notes':'Notă păstrată'})

    def tearDown(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', ResourceWarning)
            gc.collect()
        self.directory.cleanup()

    def claim(self, tech=None):
        with patch.object(scheduler,'datetime',wraps=datetime) as clock:
            clock.now.return_value = datetime(2026,10,16,8,0)
            return scheduler.claim_appointment(self.db,self.booking['id'],tech or self.reporter,'Mesaj mecanic: întârziere lipsă piese')

    def test_same_booking_moves_and_old_owner_has_no_copy(self):
        result = self.claim()
        self.assertTrue(result['changed'])
        self.assertEqual(result['appointment']['id'],self.booking['id'])
        self.assertEqual(result['appointment']['technician_id'],self.reporter)
        self.assertIn('Notă păstrată',result['appointment']['notes'])
        self.assertEqual(result['appointment']['source'],'local_schedule_override')
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM scheduler_appointments WHERE technician_id=?',(self.owner,)).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM scheduler_appointments').fetchone()[0],1)
        conn.close()

    def test_busy_receiver_gets_first_free_slot_without_changing_other_jobs(self):
        other = scheduler.create_appointment(self.db,{'technician_id':self.reporter,'start_at':'2026-10-16T08:30','end_at':'2026-10-16T12:00','registration':'B105TST','operations':'placute frana'})
        result = self.claim()
        self.assertEqual(result['appointment']['start_at'],'2026-10-16T12:00')
        self.assertEqual(result['appointment']['end_at'],'2026-10-16T14:30')
        self.assertTrue(result['slot_changed'])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT end_at FROM scheduler_appointments WHERE id=?',(other['id'],)).fetchone()[0],'2026-10-16T12:00')
        conn.close()

    def test_duplicate_message_is_idempotent(self):
        first=self.claim()
        second=self.claim()
        self.assertFalse(second['changed'])
        self.assertEqual(first['appointment']['notes'],second['appointment']['notes'])

    def test_invalid_mechanic_does_not_remove_old_assignment(self):
        with self.assertRaises(ValueError):
            self.claim(999999)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT technician_id FROM scheduler_appointments WHERE id=?',(self.booking['id'],)).fetchone()[0],self.owner)
        conn.close()


if __name__=='__main__':
    unittest.main()
