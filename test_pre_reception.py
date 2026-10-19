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

    async def test_retest_backs_up_and_restarts_only_once_without_new_order(self):
        sender = p.RECIPIENTS[0]
        p.save(self.root, sender, 'done', {**self.seed, 'order': {'id': '19528'}})
        with p.db(self.root) as conn:
            conn.execute('INSERT INTO orders VALUES(?,?,?)', (d.OFFER, 'done', '{"id":"19528"}'))
        seed = AsyncMock(return_value={**self.seed, 'retest_command_id': d.RETEST_COMMAND, 'retest_command_code': 'PHDEV-10296'})
        await p.restart(self.root, self.send, seed)
        await p.restart(self.root, self.send, seed)
        seed.assert_awaited_once_with(retest=True)
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(len(list((self.root / 'pre_reception_private').glob('before-retest-*.sqlite3'))), 1)
        self.assertFalse(p.status(self.root)['orders'])
        for target in p.RECIPIENTS:
            stage, data = p.get(self.root, target)
            self.assertEqual(stage, 'document')
            self.assertEqual(data['retest_command_id'], d.RETEST_COMMAND)
            self.assertNotIn('document_current', data)
        self.writer.assert_not_awaited()

    async def test_start_idempotent_only_two_numbers(self):
        seed = AsyncMock(return_value=self.seed)
        await p.start(self.root, self.send, seed)
        await p.start(self.root, self.send, seed)
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual({x.args[0] for x in self.send.await_args_list}, set(p.RECIPIENTS))
        self.assertFalse(p.status(self.root)['production_enabled'])

    async def test_each_document_request_clears_old_itp_and_confirmations(self):
        sender = p.RECIPIENTS[0]
        old = {**self.seed, 'document_verified': True, 'document_current': True,
               'document_fields_confirmed': True, 'identity_fields_confirmed': True,
               'itp_confirmed': True, 'final_confirmed': True, 'itp': '2026-12-10',
               'itp_source': 'document', 'client_name': 'Client Test',
               'previous_dsm': {'car_itp_expires_date': '2026-12-10'}}
        for stage, reply in [('attendance', 'DA'), ('confirm', 'CORECTEAZA')]:
            with self.subTest(stage=stage):
                p.save(self.root, sender, stage, old)
                await p.text(self.root, sender, reply, self.send, self.writer)
                new_stage, data = p.get(self.root, sender)
                self.assertEqual(new_stage, 'document')
                self.assertEqual(data['client_name'], 'Client Test')
                self.assertEqual(data['previous_dsm']['car_itp_expires_date'], '2026-12-10')
                for field in ('document_verified', 'document_current', 'itp', 'itp_confirmed', 'document_fields_confirmed', 'final_confirmed'):
                    self.assertNotIn(field, data)
                self.assertIn('Pentru actualizarea datelor', self.send.await_args.args[1])
                self.assertIn('la fiecare recepție', self.send.await_args.args[1])
        self.writer.assert_not_awaited()

    async def test_front_photo_does_not_reuse_previous_itp(self):
        sender = p.RECIPIENTS[0]
        p.save(self.root, sender, 'attendance', {**self.seed, 'itp': '2026-12-10', 'previous_dsm': {'car_itp_expires_date': '2026-12-10'}})
        await p.text(self.root, sender, 'DA', self.send, self.writer)
        reader = lambda *_: {'vin': d.VIN, 'registration': d.PLATE, 'owner_name': 'Client Test', 'manufacture_year': 2019}
        await p.image(self.root, sender, {'image': {'id': 'fresh'}}, self.send, AsyncMock(return_value=(b'image', 'image/jpeg')), reader)
        self.assertTrue(p.get(self.root, sender)[1]['document_current'])
        await p.text(self.root, sender, 'DA', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[0], 'document_itp')
        self.assertIn('fotografia clară a rubricii ITP', self.send.await_args.args[1])
        await p.text(self.root, sender, 'DA', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[0], 'document_itp')
        self.assertNotIn('itp', p.get(self.root, sender)[1])
        await p.text(self.root, sender, '10.12.2028', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[1]['itp'], '2028-12-10')
        self.writer.assert_not_awaited()

    async def test_old_confirm_stage_requests_a_new_document(self):
        sender = p.RECIPIENTS[0]
        p.save(self.root, sender, 'confirm', {**self.seed, 'document_verified': True, 'document_fields_confirmed': True, 'itp': '2026-12-10'})
        await p.text(self.root, sender, 'CONFIRM DATELE', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[0], 'document')
        self.writer.assert_not_awaited()

    async def test_unknown_phone_and_decline(self):
        self.assertFalse(await p.text(self.root, '40700100007', 'DA', self.send, self.writer))
        p.save(self.root, p.RECIPIENTS[0], 'attendance', self.seed)
        await p.text(self.root, p.RECIPIENTS[0], 'NU', self.send, self.writer)
        self.assertEqual(p.get(self.root, p.RECIPIENTS[0])[0], 'stopped')
        self.writer.assert_not_awaited()

    async def test_existing_client_is_reused_and_only_missing_address_is_requested(self):
        sender = p.RECIPIENTS[0]
        data = {**self.seed, 'client_name': 'Client Test', 'phone': '40700100006', 'itp': '2027-01-01'}
        p.save(self.root, sender, 'requirements', data)
        await p.text(self.root, sender, 'NU', self.send, self.writer)
        self.assertEqual(p.get(self.root, sender)[0], 'client_address')
        await p.text(self.root, sender, 'București, Str. Test 1', self.send, self.writer)
        stage, result = p.get(self.root, sender)
        self.assertEqual(stage, 'confirm')
        self.assertEqual(result['phone'], '40700100006')
        self.assertIn('Client Test', self.send.await_args.args[1])

    async def test_client_correction_keeps_existing_values_on_yes(self):
        sender = p.RECIPIENTS[0]
        data = {**self.seed, 'client_name': 'Client Test', 'client_address': 'București, Str. Test 1', 'phone': '40700100006', 'itp': '2027-01-01', 'extra': 'Nu'}
        p.save(self.root, sender, 'confirm', data)
        await p.text(self.root, sender, 'CORECTEAZA CLIENT', self.send, self.writer)
        for expected in ('client_address', 'phone', 'confirm'):
            await p.text(self.root, sender, 'DA', self.send, self.writer)
            self.assertEqual(p.get(self.root, sender)[0], expected)
        self.assertEqual(p.get(self.root, sender)[1]['phone'], '40700100006')
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
        for value in ['Plăcuțe față suplimentar', 'Client Test', 'Bucuresti, Str Test 1', 'DA']:
            await p.text(self.root, sender, value, self.send, self.writer)
        self.assertIn('EXPIRAT', self.send.await_args.args[1])
        self.assertIn('Ulei original BMW', self.send.await_args.args[1])
        self.assertIn('reverificate și confirmate la semnarea comenzii', self.send.await_args.args[1])
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
        data = {**self.seed, 'client': 'Test', 'client_name': 'Test', 'client_address': 'București, Str. Test 1', 'phone': p.RECIPIENTS[0], 'itp': '2027-01-01', 'extra': 'Nu', 'document_verified': True}
        data['document_current'] = True
        for sender in p.RECIPIENTS:
            p.save(self.root, sender, 'confirm', data)
        await asyncio.gather(*(p.text(self.root, sender, 'CONFIRM DATELE', self.send, self.writer) for sender in p.RECIPIENTS))
        self.assertEqual(self.writer.await_count, 1)
        self.assertEqual(len(p.status(self.root)['orders']), 1)
        await p.text(self.root, p.RECIPIENTS[0], 'CONFIRM DATELE', self.send, self.writer)
        self.assertEqual(self.writer.await_count, 1)

    async def test_uncertain_write_is_reconciled_not_recreated(self):
        data = {**self.seed, 'client': 'Test', 'client_name': 'Test', 'client_address': 'București, Str. Test 1', 'phone': p.RECIPIENTS[0], 'itp': '2027-01-01', 'extra': 'Nu', 'document_verified': True}
        data['document_current'] = True
        p.save(self.root, p.RECIPIENTS[0], 'confirm', data)
        self.writer.side_effect = RuntimeError('timeout after POST')
        await p.text(self.root, p.RECIPIENTS[0], 'CONFIRM DATELE', self.send, self.writer)
        await p.text(self.root, p.RECIPIENTS[0], 'CONFIRM DATELE', self.send, self.writer)
        self.assertTrue(self.writer.await_args_list[0].args[1])
        self.assertFalse(self.writer.await_args_list[1].args[1])


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_retest_updates_existing_command_and_returns_verified_itp(self):
        data = {'vin': d.VIN, 'registration': d.PLATE, 'final_confirmed': True, 'document_verified': True, 'document_current': True, 'document_fields_confirmed': True, 'client_name': 'Client Test', 'client_address': 'Blejoi, Prahova, România', 'phone': p.RECIPIENTS[0], 'itp': '2028-12-10', 'itp_source': 'client', 'retest_command_id': d.RETEST_COMMAND, 'work': 'Încărcare freon', 'extra': 'Nu'}
        head = {'transaction_head__id': d.RETEST_COMMAND, 'transaction_head__code': 'PHDEV-10296', 'transaction_head__status': 'working', 'transaction_head__type': '2', 'car_body_series': d.VIN, 'car_auto_number': d.PLATE, 'car_id': '10', 'contact_id': '22', 'transaction_head__description': d.requirements_description(data)}
        call = AsyncMock(return_value={'transaction': head})
        with patch.object(d, 'call', call), patch.object(d, 'source', AsyncMock()) as source, patch.object(d, 'save_vehicle_details', AsyncMock()) as vehicle, patch.object(d, 'save_client_details', AsyncMock()):
            result = await d.create_or_find(data, True)
        source.assert_not_awaited()
        call.assert_awaited_once_with('/special/transaction/get_transaction/' + d.RETEST_COMMAND)
        vehicle.assert_awaited_once_with(data, head, d.RETEST_COMMAND)
        self.assertEqual(result['itp'], data['itp'])
        self.assertEqual(result['id'], d.RETEST_COMMAND)

    async def test_retest_rejects_closed_or_other_vehicle_command(self):
        head = {'transaction_head__id': d.RETEST_COMMAND, 'transaction_head__code': 'PHDEV-10296', 'transaction_head__status': 'working', 'transaction_head__type': '2', 'car_body_series': d.VIN, 'car_auto_number': d.PLATE}
        for change in ({'transaction_head__status': 'canceled'}, {'car_body_series': 'OTHER'}, {'transaction_head__id': '19528'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                d.check_retest_order({**head, **change})

    async def test_seed_reuses_existing_client_data_and_keeps_baseline(self):
        source = {'transaction': {'contact_id': '22', 'car_id': '10'}}
        client = {'contact_name': 'Client Test', 'mobile': [{'id': 12, 'val': '0700100000'}], 'address': [{'address_id': 15, 'address_street_1': 'București, Str. Test 1'}]}
        car = {'car_body_series': d.VIN, 'car_manufacture_year': 2019, 'car_itp_expires_date': '2026-12-10'}
        with patch.object(d, 'source', AsyncMock(return_value=source)), patch.object(d, 'eligibility', return_value='accepted'), patch.object(d, 'call', AsyncMock(side_effect=[client, car])):
            result = await d.seed()
        self.assertEqual(result['client_name'], 'Client Test')
        self.assertEqual(result['phone'], '40700100000')
        self.assertEqual(result['client_address'], client['address'][0]['address_street_1'])
        self.assertEqual(result['previous_dsm']['car_itp_expires_date'], '2026-12-10')
        self.assertNotIn('itp_confirmed', result)

    async def test_only_changed_itp_is_written(self):
        car = {'car_body_series': d.VIN, 'car_manufacture_year': '2019', 'car_itp_expires_date': '2026-12-10'}
        data = {'manufacture_year': 2019, 'itp': '2028-12-10', 'previous_dsm': car}
        call = AsyncMock(side_effect=[car, {'status': 1}, {**car, 'car_itp_expires_date': data['itp']}])
        with patch.object(d, 'call', call):
            await d.save_vehicle_details(data, {'car_id': '10'}, '20000')
        self.assertEqual(call.await_args_list[1].args[1]['input'], 'car_itp_expires_date')
        self.assertEqual(call.await_count, 3)

    async def test_concurrent_dsm_change_blocks_before_writes(self):
        data = {'manufacture_year': 2019, 'itp': '2028-12-10', 'previous_dsm': {'car_itp_expires_date': '2026-12-10'}}
        call = AsyncMock(return_value={'car_body_series': d.VIN, 'car_manufacture_year': '2019', 'car_itp_expires_date': '2027-12-10'})
        with patch.object(d, 'call', call):
            with self.assertRaises(ValueError):
                await d.save_vehicle_details(data, {'car_id': '10'}, '20000')
        self.assertEqual(call.await_count, 1)

    async def test_no_confirmation_no_write(self):
        with patch.object(d, 'call', new_callable=AsyncMock) as call:
            with self.assertRaises(ValueError):
                await d.create_or_find({'vin': d.VIN, 'registration': d.PLATE}, True)
            call.assert_not_awaited()

    async def test_signed_source_transforms_once_and_separates_vehicle_data(self):
        head = {'transaction_head__id': d.OFFER, 'transaction_head__type': '1', 'transaction_head__status': 'working', 'car_body_series': d.VIN, 'car_auto_number': d.PLATE}
        source = {'transaction': head, 'sms_or_qrcode_sended': {'service_offer': 'complete'}, 'transaction_head_childs': []}
        order = {'transaction': {**head, 'transaction_head__id': '20000', 'transaction_head__parent': d.OFFER, 'transaction_head__type': '2', 'transaction_head__code': 'PHDEV-1', 'car_id': '10'}}
        data = {'vin': d.VIN, 'registration': d.PLATE, 'document_verified': True, 'document_fields_confirmed': True, 'final_confirmed': True, 'client': 'Test', 'phone': p.RECIPIENTS[0], 'itp': '2027-01-01', 'manufacture_year': 2019, 'work': 'Încărcare freon', 'extra': 'Verificare plăcuțe'}
        data.update(client_name='Test', client_address='București, Str. Test 1', document_current=True)
        desired = d.requirements_description(data)
        marked = {'transaction': {**order['transaction'], 'transaction_head__description': desired}}
        car = {'car_body_series': d.VIN}
        saved = {**car, 'car_itp_expires_date': data['itp'], 'car_manufacture_year': '2019'}
        call = AsyncMock(side_effect=[source, {'status': 1, 'new_transaction_id': '20000'}, order, car, {'status': 1}, {'status': 1}, saved, {'status': 1}, marked])
        with patch.object(d, 'call', call), patch.object(d, 'save_client_details', AsyncMock()) as client_writer:
            result = await d.create_or_find(data, True)
        client_writer.assert_awaited_once_with(data, order['transaction'], '20000')
        self.assertEqual(result['code'], 'PHDEV-1')
        self.assertEqual(call.await_args_list[1].args[1]['transform_type'], 'estimate')
        self.assertEqual(call.await_args_list[1].args[1]['payment_advance'], 'false')
        self.assertEqual(call.await_args_list[4].args[1]['input'], 'car_itp_expires_date')
        self.assertEqual(call.await_args_list[5].args[1]['input'], 'car_manufacture_year')
        self.assertEqual(call.await_args_list[7].args[1]['transaction_head__description'], desired)
        self.assertIn('ÎMPREUNĂ CU CONSILIERUL', desired)
        for excluded in ('Client confirmat:', 'Telefon', 'Titular', 'ITP', 'TEST', 'WhatsApp', '2019'):
            self.assertNotIn(excluded, desired)

    async def test_existing_order_is_cleaned_without_creating_another(self):
        data = {'vin': d.VIN, 'registration': d.PLATE, 'document_verified': True, 'document_fields_confirmed': True, 'final_confirmed': True, 'itp': '2027-01-01', 'manufacture_year': 2019, 'work': 'Freon', 'extra': 'Nu'}
        data.update(client_name='Test', client_address='București, Str. Test 1', phone=p.RECIPIENTS[0], document_current=True)
        head = {'transaction_head__type': '2', 'transaction_head__parent': d.OFFER, 'car_body_series': d.VIN, 'car_id': '10', 'transaction_head__description': 'Freon\n' + d.LEGACY_MARKER + '\nTelefon confirmat: 123\nITP: vechi'}
        source = {'transaction_head_childs': [{'transaction_id': '20000', 'transaction_head__type': '2'}]}
        desired = d.requirements_description(data, head['transaction_head__description'])
        car = {'car_body_series': d.VIN, 'car_manufacture_year': '2019', 'car_itp_expires_date': data['itp']}
        call = AsyncMock(side_effect=[{'transaction': head}, car, car, {'status': 1}, {'transaction': {**head, 'transaction_head__description': desired}}])
        with patch.object(d, 'source', AsyncMock(return_value=source)), patch.object(d, 'call', call), patch.object(d, 'save_client_details', AsyncMock()):
            await d.create_or_find(data, False)
        self.assertEqual(desired, 'Freon\n' + d.RECHECK_NOTICE)
        self.assertEqual(d.requirements_description(data, desired), desired)
        self.assertFalse(any('offer_accepted' in c.args[0] for c in call.await_args_list))

    async def test_vehicle_mismatch_or_unconfirmed_save_blocks(self):
        data = {'itp': '2027-01-01', 'manufacture_year': 2019}
        for responses in ([{'car_body_series': 'WRONG'}],
                          [{'car_body_series': d.VIN}, {'status': 1}, {'status': 1}, {'car_body_series': d.VIN}],
                          [{'car_body_series': d.VIN}, {'status': 1, 'validation': {'car_itp_expires_date': 'invalid'}}]):
            with self.subTest(responses=responses), patch.object(d, 'call', AsyncMock(side_effect=responses)):
                with self.assertRaises(ValueError):
                    await d.save_vehicle_details(data, {'car_id': '10'}, '20000')

    async def test_client_fields_saved_to_client_and_retries_skip_updates(self):
        data = {'client_name': 'Client Test', 'client_address': 'București, Str. Test 1', 'phone': '0700100000'}
        old = {'contact_name': 'Vechi', 'mobile': [{'id': 12, 'val': '0700100008', 'checked': 'checked'}], 'address': [{'address_id': 15, 'address_street_1': 'Adresă veche'}]}
        saved = {'contact_name': data['client_name'], 'mobile': [{'id': 12, 'val': '40700100000'}], 'address': [{'address_id': 15, 'address_street_1': data['client_address']}]}
        call = AsyncMock(side_effect=[old, {'status': 1}, {'status': 1}, {'status': 1}, saved, saved, saved])
        with patch.object(d, 'call', call):
            await d.save_client_details(data, {'contact_id': 22}, '20000')
            await d.save_client_details(data, {'contact_id': 22}, '20000')
        updates = [c for c in call.await_args_list if c.args[0].endswith('update_contact_detail')]
        self.assertEqual([c.args[1]['input'] for c in updates], ['client_name', 'client_phone', 'client_address_street'])
        self.assertEqual(updates[1].args[1]['id'], '12')
        self.assertEqual(updates[2].args[1]['id'], '15')
        self.assertTrue(all(c.args[1]['contact_id'] == '22' and c.args[1]['transaction_id'] == '20000' for c in updates))

    async def test_client_failed_verification_or_validation_blocks(self):
        data = {'client_name': 'Client Test', 'client_address': 'București, Str. Test 1', 'phone': p.RECIPIENTS[0]}
        for responses in ([{}, {'status': 1, 'validation': {'client_name': 'invalid'}}],
                          [{}, {'status': 1}, {'status': 1}, {'status': 1}, {}]):
            with self.subTest(responses=responses), patch.object(d, 'call', AsyncMock(side_effect=responses)):
                with self.assertRaises(ValueError):
                    await d.save_client_details(data, {'contact_id': 22}, '20000')

    async def test_legacy_combined_client_data_blocks_before_order_creation(self):
        data = {'vin': d.VIN, 'registration': d.PLATE, 'document_verified': True, 'document_fields_confirmed': True, 'final_confirmed': True, 'itp': '2027-01-01', 'client': 'Test, Str. Test 1', 'phone': p.RECIPIENTS[0]}
        with patch.object(d, 'call', AsyncMock()) as call:
            with self.assertRaises(ValueError):
                await d.create_or_find(data, True)
        call.assert_not_awaited()

    async def test_old_document_confirmation_cannot_create_order(self):
        data = {'vin': d.VIN, 'registration': d.PLATE, 'document_verified': True, 'document_fields_confirmed': True, 'final_confirmed': True, 'itp': '2027-01-01', 'client_name': 'Client Test', 'client_address': 'București, Str. Test 1', 'phone': p.RECIPIENTS[0]}
        with patch.object(d, 'call', AsyncMock()) as call:
            with self.assertRaises(ValueError):
                await d.create_or_find(data, True)
        call.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
