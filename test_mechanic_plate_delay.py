import unittest
from unittest.mock import AsyncMock, patch
import app


class MechanicPlateDelayTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, candidates):
        technician = {'id': 2, 'name': 'ADELIN'}
        send = AsyncMock()
        claim = AsyncMock(side_effect=lambda tech,sender,body,row: {**row,'technician_id':tech['id']})
        with patch.object(app, '_supersede_mechanic_reference'), patch.object(app, '_claim_mechanic_booking', claim), patch.object(app, '_technician_for_sender', return_value=technician), patch.object(app, '_is_beta_test_phone', return_value=False), patch.object(app, '_mechanic_plate_candidates', return_value=candidates), patch.object(app, '_pending_mechanic_delay', return_value=None), patch.object(app, '_record_mechanic_message') as record, patch.object(app, '_log_mechanic_message_for_request'), patch.object(app, 'send_whatsapp_text', send), patch.object(app, 'scheduler_mark_incomplete') as move, patch.object(app, '_route_new_vehicle_intake', AsyncMock()) as intake:
            await app._route_buffered_message('40700100004', 'B104TST intarziere lipsa piese')
            intake.assert_not_awaited()
            move.assert_not_called()
        return record, send, claim

    async def test_other_mechanics_car_is_claimed_before_asking_delay_duration(self):
        record, send, claim = await self.run_case([{'id':595,'technician_id':6,'registration':'B104TST','job_number':'APP-1887'}])
        self.assertEqual(record.call_args.args[0], 2)
        self.assertEqual(record.call_args.args[3:], (595,'delay','awaiting_duration'))
        claim.assert_awaited_once()
        self.assertIn('Câte ore',send.call_args.args[1])

    async def test_own_car_asks_duration_before_moving_anything(self):
        record, send, claim = await self.run_case([{'id':595,'technician_id':2,'registration':'B104TST','job_number':'APP-1887'}])
        claim.assert_not_awaited()
        self.assertEqual(record.call_args.args[3:], (595,'delay','awaiting_duration'))
        self.assertIn('Câte ore',send.call_args.args[1])

    async def test_missing_or_ambiguous_car_is_not_sent_to_customer_intake(self):
        for candidates in ([], [{'id':1,'technician_id':2},{'id':2,'technician_id':2}]):
            record, _, claim = await self.run_case(candidates)
            claim.assert_not_awaited()
            self.assertEqual(record.call_args.args[3:], (None,'delay_plate','needs_review'))

    async def test_customer_number_is_not_given_mechanic_access(self):
        with patch.object(app, '_technician_for_sender', return_value=None), patch.object(app, '_is_beta_test_phone', return_value=False), patch.object(app, '_mechanic_plate_candidates') as lookup:
            self.assertFalse(await app._route_mechanic_message('40700100005','B104TST intarziere lipsa piese'))
            lookup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
