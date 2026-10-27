"""Staff delay reports must reach the scheduler before customer intake."""
from contextlib import closing
from datetime import datetime
import gc
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import app
from scheduler import init_scheduler_db, save_technician


class MechanicDelayIntentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.path=Path(self.temp.name)/'schedule.db'
        init_scheduler_db(self.path)
        self.sender='40700100011'
        mechanic=save_technician(self.path,dict(name='MECANIC TEST',phone=self.sender,skills={'diagnoza':5}))
        day=datetime.now().date().isoformat()
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute("""INSERT INTO scheduler_appointments
                (id,technician_id,start_at,end_at,status,registration,job_number,operations,estimated_hours)
                VALUES(1,?,?,?,'programat','B117TST','B117TST / APP-1971','Diagnoză',1)""",
                (mechanic['id'],day+'T08:30',day+'T09:30'))
        for mock in (patch.object(app,'DB_PATH',self.path),patch.object(app,'_is_beta_test_phone',return_value=False)):
            mock.start();self.addCleanup(mock.stop)

    def test_delay_spellings_and_unrelated_messages(self):
        for message in ('B117TST INTRAZIERE 1h','B117TST întârziere 1 oră',
                        'APP-1971 INTARZIE 30 minute','B117TST neterminată'):
            with self.subTest(message=message):
                self.assertTrue(app._looks_like_mechanic_delay(message))
        for message in ('B117TST schimb ulei','Vreau o programare','B117TST filtru interior'):
            self.assertFalse(app._looks_like_mechanic_delay(message))
        self.assertEqual(app._mechanic_duration_hours('ÎNTÂRZIERE 1 oră'),1)
        self.assertEqual(app._mechanic_duration_hours('întârziere 30 minute'),0.5)

    async def test_exact_screenshot_message_bypasses_customer_intake_and_records_one_hour(self):
        with patch.object(app,'send_whatsapp_text',AsyncMock()) as send, \
             patch.object(app,'_route_new_vehicle_intake',AsyncMock()) as intake:
            await app._route_buffered_message(self.sender,'B117TST INTRAZIERE 1h')
            intake.assert_not_awaited()
            self.assertEqual(send.await_count,1)
            self.assertEqual(send.await_args.args[0],self.sender)
            self.assertIn('întârzierea de 1 ore',send.await_args.args[1])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM scheduler_appointments WHERE id=1').fetchone()[0],'neterminat')
            self.assertEqual(conn.execute('SELECT action,status,appointment_id FROM scheduler_mechanic_messages').fetchall(),[('delay','processed',1)])
            self.assertEqual(conn.execute('SELECT SUM(estimated_hours) FROM scheduler_appointments WHERE continued_from_id=1').fetchone()[0],1)

    async def test_typo_without_duration_is_linked_for_review_and_followup(self):
        with patch.object(app,'send_whatsapp_text',AsyncMock()):
            self.assertTrue(await app._route_mechanic_message(self.sender,'B117TST INTRAZIERE lipsă piese'))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT status,appointment_id FROM scheduler_mechanic_messages').fetchone(),('awaiting_duration',1))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM scheduler_appointments WHERE continued_from_id=1').fetchone()[0],0)

    async def test_unknown_sender_cannot_change_schedule_using_delay_keyword(self):
        with patch.object(app,'send_whatsapp_text',AsyncMock()) as send:
            self.assertFalse(await app._route_mechanic_message('40700100012','B117TST INTRAZIERE 1h'))
            send.assert_not_awaited()
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM scheduler_appointments WHERE id=1').fetchone()[0],'programat')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM scheduler_mechanic_messages').fetchone()[0],0)

    async def test_authorized_beta_sender_can_report_delay_by_unique_plate(self):
        with patch.object(app,'_is_beta_test_phone',return_value=True),patch.object(app,'send_whatsapp_text',AsyncMock()) as send:
            self.assertTrue(await app._route_mechanic_message('40700100012','B117TST INTRAZIERE 1h'))
            self.assertIn('întârzierea de 1 ore',send.await_args.args[1])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM scheduler_appointments WHERE id=1').fetchone()[0],'neterminat')

    async def test_authorized_beta_sender_does_not_guess_between_duplicate_bookings(self):
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute("""INSERT INTO scheduler_appointments
                (id,technician_id,start_at,end_at,status,registration,job_number,operations,estimated_hours)
                SELECT 2,technician_id,start_at,end_at,status,registration,'APP-1972',operations,estimated_hours
                FROM scheduler_appointments WHERE id=1""")
        with patch.object(app,'_is_beta_test_phone',return_value=True),patch.object(app,'send_whatsapp_text',AsyncMock()) as send:
            self.assertTrue(await app._route_mechanic_message('40700100012','B117TST INTRAZIERE 1h'))
            self.assertIn('nu am găsit o singură programare',send.await_args.args[1])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scheduler_appointments WHERE status='programat'").fetchone()[0],2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM scheduler_mechanic_messages').fetchone()[0],0)


if __name__=='__main__':unittest.main()
