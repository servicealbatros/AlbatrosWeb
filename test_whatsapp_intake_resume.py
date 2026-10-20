import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app


class IntakeResumeTests(unittest.IsolatedAsyncioTestCase):
    def test_bodyshop_work_is_not_mistaken_for_a_customer_name(self):
        for text in ('Vopsit bară față bară spate', 'Vopsire bara fata si bara spate',
                     'Tinichigerie aripa', 'Îndreptare portieră', 'Retuș vopsea',
                     'Polish caroserie', 'Reparatie bara fata'):
            with self.subTest(text=text):
                self.assertTrue(app._has_concrete_work_request(text))
        for text in ('Ion Popescu', 'Bună ziua', 'Vreau o programare'):
            self.assertFalse(app._has_concrete_work_request(text))

    async def test_paint_both_bumpers_after_talon_creates_one_bodyshop_request(self):
        work = 'Vopsit bară față bară spate'
        flow = {'stage':'vehicle_identified','case_id':'new-b14','registration':'B111TST',
                'vin':'WVW00000000000007','vehicle':'Test vehicle','identification_source':'talon'}
        states = {'test':flow}
        send = AsyncMock()
        with patch.object(app,'_new_vehicle_states',return_value=states), patch.object(app,'_save_new_vehicle_states'), \
             patch.object(app,'send_whatsapp_text',send), patch.object(app,'create_request',return_value=51) as create, \
             patch.object(app,'_request_by_id',return_value={'id':51,'symptom':work}):
            self.assertTrue(await app._route_new_vehicle_intake('test',work))
            saved = create.call_args.args[0]
            self.assertEqual(saved['symptom'],work)
            self.assertEqual(saved['registration'],'B111TST')
            self.assertEqual(saved['category'],'TINICHIGERIE–VOPSITORIE')
            self.assertNotIn('customer_name',flow)
            self.assertNotIn('Pentru ce lucrare',send.call_args.args[1])
            self.assertTrue(await app._route_new_vehicle_intake('test',work))
            create.assert_called_once()
            self.assertIn('deja înregistrată',send.call_args.args[1])

    def test_bodyshop_work_before_plate_is_retained_without_duplication(self):
        flow={}
        app._remember_pending_work(flow,'Vopsit bară față bară spate B111TST')
        self.assertIn('Vopsit bară față bară spate',flow['pending_symptom'])
        self.assertNotIn('B111TST',flow['pending_symptom'])
        app._remember_pending_work(flow,'Vopsit bară față bară spate')
        self.assertNotIn(';',flow['pending_symptom'])

    def test_restart_restores_persisted_case_not_an_old_phone_request(self):
        flow = {'case_id': 'whatsapp:test:case:current', 'registration': 'B101TST'}
        with patch.dict(app._active_case_by_sender, {}, clear=True), patch.dict(app._plate_by_sender, {}, clear=True), patch.object(app, '_new_vehicle_states', return_value={'test': flow}):
            self.assertEqual(app._get_or_create_case('test'), flow['case_id'])
            self.assertEqual(app._plate_by_sender['test'], 'B101TST')
            self.assertNotEqual(app._get_or_create_case('different'), flow['case_id'])

    async def test_mileage_updates_current_request_without_new_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'test.db'
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE service_requests(id INTEGER PRIMARY KEY, mileage TEXT, symptom TEXT)')
                conn.executemany('INSERT INTO service_requests VALUES(?,?,?)', [(40, '', 'Revizie completa'), (39, '123', 'Alta lucrare')])
            conn.close()
            flow = {'stage': 'request_created', 'request_id': 40, 'case_id': 'current', 'registration': 'B101TST', 'pending_symptom': 'Revizie completa'}
            send = AsyncMock()
            with patch.object(app, 'DB_PATH', db), patch.object(app, '_new_vehicle_states', return_value={'test': flow}), patch.object(app, '_save_new_vehicle_states'), patch.object(app, '_request_by_id', return_value={'id': 40}), patch.object(app, 'send_whatsapp_text', send), patch.object(app, 'create_request') as create:
                self.assertTrue(await app._route_new_vehicle_intake('test', '100000km'))
                create.assert_not_called()
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute('SELECT mileage,symptom FROM service_requests WHERE id=40').fetchone(), ('100000', 'Revizie completa'))
                self.assertEqual(conn.execute('SELECT mileage FROM service_requests WHERE id=39').fetchone()[0], '123')
            conn.close()
            self.assertEqual(flow['mileage_km'], 100000)
            self.assertIn('B101TST', send.call_args.args[1])
            self.assertNotIn('Ce lucrare', send.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
