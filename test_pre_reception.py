import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
import pre_reception as p
import dsm_pre_reception as d


class PilotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.seed = {'offer_id': d.OFFER, 'vin': d.VIN, 'registration': d.PLATE, 'work': 'Ulei original BMW + filtre'}
        self.send = AsyncMock()
        self.writer = AsyncMock(return_value={'id': '20000', 'code': 'PHDEV-TEST'})

    def tearDown(self):
        self.temp.cleanup()

    async def test_start_idempotent_only_two_numbers(self):
        seed = AsyncMock(return_value=self.seed)
        await p.start(self.root, self.send, seed)
        await p.start(self.root, self.send, seed)
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual({x.args[0] for x in self.send.await_args_list}, set(p.RECIPIENTS))
        self.assertFalse(p.status(self.root)['production_enabled'])

    async def test_unknown_phone_and_decline(self):
        self.assertFalse(await p.text(self.root, '40700100007', 'DA', self.send, self.writer))
        p.save(self.root, p.RECIPIENTS[0], 'attendance', self.seed)
        await p.text(self.root, p.RECIPIENTS[0], 'NU', self.send, self.writer)
        self.assertEqual(p.get(self.root, p.RECIPIENTS[0])[0], 'stopped')
        self.writer.assert_not_awaited()

    async def test_document_mismatch_blocks(self):
        sender = p.RECIPIENTS[0]
        p.save(self.root, sender, 'document', self.seed)
        reader = lambda *_: {'vin': 'WBA00000000000003', 'registration': d.PLATE, 'itp_expires': '2028-12-01', 'itp_evidence': 'ITP'}
        await p.image(self.root, sender, {'image': {'id': '1'}}, self.send, AsyncMock(return_value=(b'image', 'image/jpeg')), reader)
        self.assertEqual(p.get(self.root, sender)[0], 'document')
        self.assertEqual(len(list((self.root / 'pre_reception_private' / 'documents').glob('*.img'))), 1)
        self.assertIn('VIN citit:', self.send.await_args.args[1])
        self.assertIn('WBA00000000000003', self.send.await_args.args[1])
        self.writer.assert_not_awaited()

    async def test_front_then_itp_and_final_gate(self):
        sender = p.RECIPIENTS[0]
        p.save(self.root, sender, 'document', self.seed)
        read = lambda *_: {'vin': d.VIN, 'registration': d.PLATE}
        download = AsyncMock(return_value=(b'image', 'image/jpeg'))
        await p.image(self.root, sender, {'image': {'id': '1'}}, self.send, download, read)
        self.assertEqual(p.get(self.root, sender)[0], 'document_check')
        read = lambda *_: {'itp_expires': '2025-01-01', 'itp_evidence': 'ITP expira 01.01.2025'}
        await p.image(self.root, sender, {'image': {'id': '2'}}, self.send, download, read)
        self.assertEqual(p.get(self.root, sender)[0], 'document_check')
        await p.text(self.root, sender, 'Client Test', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[0], 'document_itp')
        await p.text(self.root, sender, 'DA', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[0], 'document_year')
        await p.text(self.root, sender, '2015', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[0], 'requirements')
        for value in ['Plăcuțe față suplimentar', 'Client Test, Bucuresti, Str Test 1', 'DA']:
            await p.text(self.root, sender, value, self.send, self.writer)
        self.assertIn('EXPIRAT', self.send.await_args.args[1])
        self.assertIn('Ulei original BMW', self.send.await_args.args[1])
        self.writer.assert_not_awaited()
        await p.text(self.root, sender, 'DA', self.send, self.writer)
        self.writer.assert_not_awaited()
        await p.text(self.root, sender, 'CONFIRM DATELE', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[0], 'done')
        self.assertTrue(self.writer.await_args.args[1])

    async def test_three_document_questions_in_order_then_requirements(self):
        sender = p.RECIPIENTS[0]
        p.save(self.root, sender, 'document', self.seed)
        reader = lambda *_: {'vin': d.VIN, 'registration': d.PLATE, 'owner_name': 'Client Test', 'manufacture_year': 2015, 'itp_expires': '2024-07-01', 'itp_evidence': 'ITP 01.07.2024'}
        await p.image(self.root, sender, {'image': {'id': 'full'}}, self.send, AsyncMock(return_value=(b'image', 'image/jpeg')), reader)
        self.assertEqual(self.send.await_count, 1)
        self.assertTrue(self.send.await_args.args[1].startswith('1/3'))
        await p.text(self.root, sender, 'DA', self.send, self.writer)
        self.assertEqual(self.send.await_count, 2)
        self.assertTrue(self.send.await_args.args[1].startswith('2/3'))
        await p.text(self.root, sender, 'DA', self.send, self.writer)
        self.assertEqual(self.send.await_count, 3)
        self.assertTrue(self.send.await_args.args[1].startswith('3/3'))
        self.writer.assert_not_awaited()
        await p.text(self.root, sender, 'DA', self.send, self.writer)
        stage, data = p.get(self.root, sender)
        self.assertEqual(stage, 'requirements')
        self.assertTrue(data['document_fields_confirmed'])
        self.assertIn('cerințe', self.send.await_args.args[1])
        self.writer.assert_not_awaited()

    async def test_itp_by_message_is_declared_and_invalid_dates_rejected(self):
        sender = p.RECIPIENTS[0]
        p.save(self.root, sender, 'itp', {**self.seed, 'document_verified': True})
        await p.text(self.root, sender, '31.02.2024', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[0], 'itp')
        await p.text(self.root, sender, '01.07.2024', self.send, self.writer)
        stage, data = p.get(self.root, sender)
        self.assertEqual(stage, 'document_check')
        self.assertEqual(data['itp'], '2024-07-01')
        self.assertEqual(data['itp_source'], 'client')
        self.writer.assert_not_awaited()

    async def test_same_vin_historic_plate_and_expired_itp_do_not_block(self):
        sender = p.RECIPIENTS[0]
        p.save(self.root, sender, 'document', self.seed)
        reader = lambda *_: {'vin': d.VIN, 'registration': 'B108TST', 'itp_expires': '2024-07-01', 'itp_evidence': 'Valabil ITP 01.07.2024'}
        await p.image(self.root, sender, {'image': {'id': 'old'}}, self.send, AsyncMock(return_value=(b'image', 'image/jpeg')), reader)
        stage, data = p.get(self.root, sender)
        self.assertEqual(stage, 'document_check')
        self.assertEqual(data['itp'], '2024-07-01')
        self.assertEqual(data['registration'], d.PLATE)
        self.assertEqual(data['photo_registration'], 'B108TST')
        self.assertTrue(data['document_verified'])
        self.writer.assert_not_awaited()

    async def test_two_phones_one_order_restart_safe(self):
        data = {**self.seed, 'client': 'Test', 'phone': p.RECIPIENTS[0], 'itp': '2027-01-01', 'extra': 'Nu', 'document_verified': True}
        for sender in p.RECIPIENTS:
            p.save(self.root, sender, 'confirm', data)
        await asyncio.gather(*(p.text(self.root, sender, 'CONFIRM DATELE', self.send, self.writer) for sender in p.RECIPIENTS))
        self.assertEqual(self.writer.await_count, 1)
        self.assertEqual(len(p.status(self.root)['orders']), 1)
        await p.text(self.root, p.RECIPIENTS[0], 'CONFIRM DATELE', self.send, self.writer)
        self.assertEqual(self.writer.await_count, 1)

    async def test_uncertain_write_is_reconciled_not_recreated(self):
        data = {**self.seed, 'client': 'Test', 'phone': p.RECIPIENTS[0], 'itp': '2027-01-01', 'extra': 'Nu', 'document_verified': True}
        p.save(self.root, p.RECIPIENTS[0], 'confirm', data)
        self.writer.side_effect = RuntimeError('timeout after POST')
        await p.text(self.root, p.RECIPIENTS[0], 'CONFIRM DATELE', self.send, self.writer)
        await p.text(self.root, p.RECIPIENTS[0], 'CONFIRM DATELE', self.send, self.writer)
        self.assertTrue(self.writer.await_args_list[0].args[1])
        self.assertFalse(self.writer.await_args_list[1].args[1])


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_confirmation_no_write(self):
        with patch.object(d, 'call', new_callable=AsyncMock) as call:
            with self.assertRaises(ValueError):
                await d.create_or_find({'vin': d.VIN, 'registration': d.PLATE}, True)
            call.assert_not_awaited()

    async def test_signed_source_transforms_once_and_marks_test(self):
        head = {'transaction_head__id': d.OFFER, 'transaction_head__type': '1', 'transaction_head__status': 'working', 'car_body_series': d.VIN, 'car_auto_number': d.PLATE}
        source = {'transaction': head, 'sms_or_qrcode_sended': {'service_offer': 'complete'}, 'transaction_head_childs': []}
        order = {'transaction': {**head, 'transaction_head__id': '20000', 'transaction_head__parent': d.OFFER, 'transaction_head__type': '2', 'transaction_head__code': 'PHDEV-1', 'car_id': '10'}}
        marked = {'transaction': {**order['transaction'], 'transaction_head__description': '[ALBATROS PRE-RECEPTIE TEST OFERTA-3415]'}}
        call = AsyncMock(side_effect=[source, {'status': 1, 'new_transaction_id': '20000'}, order, {'status': 1}, marked])
        data = {'vin': d.VIN, 'registration': d.PLATE, 'document_verified': True, 'document_fields_confirmed': True, 'final_confirmed': True, 'client': 'Test', 'phone': p.RECIPIENTS[0], 'itp': '2027-01-01'}
        with patch.object(d, 'call', call):
            result = await d.create_or_find(data, True)
        self.assertEqual(result['code'], 'PHDEV-1')
        self.assertEqual(call.await_args_list[1].args[1]['transform_type'], 'estimate')
        self.assertEqual(call.await_args_list[1].args[1]['payment_advance'], 'false')
        self.assertIn('TEST', call.await_args_list[3].args[1]['transaction_head__description'])


if __name__ == '__main__':
    unittest.main()
