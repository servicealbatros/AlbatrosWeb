import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import app
import scheduler


class MechanicSplitCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name)/'test.db'
        scheduler.init_scheduler_db(self.db)
        self.tech = scheduler.save_technician(self.db, {'name':'MIHAI','skills':{'diagnoza':5,'electrica':5}})
        self.booking = scheduler.create_appointment(self.db,{'technician_id':self.tech['id'],'start_at':'2026-10-16T08:30','end_at':'2026-10-16T11:00','registration':'B107TST','job_number':'APP-1904','operations':'Diagnoza dificila'})
        self.patches = [patch.object(app,'DB_PATH',self.db), patch.object(app,'_technician_for_sender',return_value=self.tech), patch.object(app,'_is_beta_test_phone',return_value=False), patch.object(app,'_mechanic_plate_candidates',return_value=[self.booking]), patch.object(app,'_log_mechanic_message_for_request'), patch.object(app,'send_whatsapp_text',AsyncMock())]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        gc.collect()
        self.temp.cleanup()

    async def test_ready_then_plate_completes_instead_of_entering_customer_intake(self):
        self.assertTrue(await app._route_mechanic_message('40700100004','Gata'))
        self.assertIsNotNone(app._pending_mechanic_reference(self.tech['id'],'40700100004'))
        self.assertTrue(await app._route_mechanic_message('40700100004','B107TST'))
        self.assertEqual(scheduler.get_appointment(self.db,self.booking['id'])['status'],'finalizat')
        self.assertIsNone(app._pending_mechanic_reference(self.tech['id'],'40700100004'))
        self.assertIn('finalizată',app.send_whatsapp_text.call_args.args[1])

    async def test_ready_overrides_pending_delay_and_clears_old_delay(self):
        app._record_mechanic_message(self.tech['id'],'40700100004','Intarziere B107TST',self.booking['id'],'delay','awaiting_duration')
        await self.test_ready_then_plate_completes_instead_of_entering_customer_intake()
        self.assertIsNone(app._pending_mechanic_delay(self.tech['id'],'40700100004'))

    async def test_delay_then_plate_keeps_duration_pending_and_flags_booking(self):
        self.assertTrue(await app._route_mechanic_message('40700100004','Intarziere lipsa piese'))
        self.assertTrue(await app._route_mechanic_message('40700100004','B107TST'))
        pending = app._pending_mechanic_delay(self.tech['id'],'40700100004')
        self.assertEqual(pending['appointment_id'],self.booking['id'])
        self.assertTrue(scheduler.list_appointments(self.db,'2026-10-16','2026-10-17')[0]['delay_reported'])

    async def test_expired_command_cannot_finish_car(self):
        await app._route_mechanic_message('40700100004','Gata')
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE scheduler_mechanic_messages SET created_at=datetime('now','-31 minutes')")
        conn.close()
        self.assertFalse(await app._route_mechanic_message('40700100004','B107TST'))
        self.assertEqual(scheduler.get_appointment(self.db,self.booking['id'])['status'],'programat')

    async def test_pending_command_is_private_to_sender(self):
        await app._route_mechanic_message('40700100004','Gata')
        self.assertFalse(await app._route_mechanic_message('40700100005','B107TST'))
        self.assertEqual(scheduler.get_appointment(self.db,self.booking['id'])['status'],'programat')

    async def test_existing_legacy_missing_reference_command_can_resume(self):
        app._record_mechanic_message(self.tech['id'],'40700100004','Gata',None,'delay_missing_reference','needs_review')
        self.assertTrue(await app._route_mechanic_message('40700100004','B107TST'))
        self.assertEqual(scheduler.get_appointment(self.db,self.booking['id'])['status'],'finalizat')


    async def test_started_then_app_reference_marks_in_progress(self):
        self.assertTrue(await app._route_mechanic_message('40700100004','Pornita'))
        self.assertTrue(await app._route_mechanic_message('40700100004','APP-1904'))
        self.assertEqual(scheduler.get_appointment(self.db,self.booking['id'])['status'],'in_lucru')

    async def test_explicit_command_supersedes_previous_missing_reference(self):
        await app._route_mechanic_message('40700100004','Gata')
        self.assertTrue(await app._route_mechanic_message('40700100004','Pornita B107TST'))
        self.assertIsNone(app._pending_mechanic_reference(self.tech['id'],'40700100004'))
        self.assertEqual(scheduler.get_appointment(self.db,self.booking['id'])['status'],'in_lucru')


if __name__ == '__main__':
    unittest.main()



