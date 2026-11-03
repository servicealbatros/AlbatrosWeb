import tempfile
import unittest
from unittest.mock import patch

import dsm_advisor_order as writer


class AdvisorOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = writer.AdvisorOrderInput(operation_id='advisor-operation-123',
            source_id='123', car_id='456', vin='WBA00000000000002', phone='40700100001',
            previous_registration='B101TST', registration='B102TST', work='Verificare turbină')
        self.registration = 'B101TST'
        self.description = ''
        self.created = 0
        self.uncertain = False
        self.bad_phone = False
        self.bad_vin = False
        self.active = False
        self.writes = []
        self.patches = [patch.object(writer, 'call', side_effect=self.call),
                        patch.object(writer, '_search_transactions', side_effect=self.search),
                        patch.object(writer, '_read_details', side_effect=self.details),
                        patch.object(writer, '_evaluate', side_effect=self.update_plate)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def head(self, ident):
        return dict(transaction_head__id=ident, transaction_head__type='2',
            transaction_head__status='working', car_id='456', contact_id='789',
            car_body_series='WBA00000000000003' if self.bad_vin else self.data.vin,
            car_auto_number=self.registration, transaction_head__code='PHDEV-TEST',
            transaction_head__description=self.description)

    async def search(self, registration):
        return [dict(transaction_id='999', transaction_type='2', status='working')] if self.active else []

    async def details(self, ids):
        return {i: {'transaction': self.head(i)} for i in ids}

    async def call(self, path, data=None):
        if '/get_transaction/' in path:
            return {'transaction': self.head(path.rsplit('/', 1)[1])}
        if path.endswith('/get_car'):
            return {'car_body_series': self.data.vin, 'car_auto_number': self.registration}
        if path.endswith('/get_client'):
            return {'mobile': [{'val': '0700100002' if self.bad_phone else '0700100001'}]}
        self.writes.append(path)
        if path.endswith('/start_transaction'):
            self.created += 1
            if self.uncertain:
                raise RuntimeError('Connection lost after write')
            return {'transaction_id': '999'}
        if path.endswith('/update_transaction_details'):
            self.description = data['transaction_head__description']
        return {'status': 1}

    async def update_plate(self, expression):
        self.assertIn('"input": "car_no"', expression)
        self.writes.append('plate')
        self.registration = self.data.registration
        return {'status': 'success'}

    async def test_preview_has_no_writes_and_create_retry_has_one_order(self):
        result = await writer.run(self.folder.name, self.data)
        self.assertTrue(result['ready'])
        self.assertEqual(self.writes, [])
        self.data.dry_run = False
        result = await writer.run(self.folder.name, self.data)
        self.assertEqual(result['code'], 'PHDEV-TEST')
        self.assertEqual(self.registration, 'B102TST')
        self.assertEqual(self.description, self.data.work)
        self.active = True
        result = await writer.run(self.folder.name, self.data)
        self.assertTrue(result['verified'])
        self.assertEqual(self.created, 1)

    async def test_identity_mismatch_never_writes(self):
        self.data.dry_run = False
        for field in ['bad_phone', 'bad_vin']:
            setattr(self, field, True)
            with self.assertRaises(ValueError):
                await writer.run(self.folder.name, self.data)
            setattr(self, field, False)
        self.assertEqual(self.writes, [])

    async def test_existing_order_does_not_create_or_change_it(self):
        self.active = True
        self.data.dry_run = False
        result = await writer.run(self.folder.name, self.data)
        self.assertTrue(result['review_required'])
        self.assertEqual(self.writes, [])

    async def test_uncertain_creation_is_not_retried(self):
        self.uncertain = True
        self.data.dry_run = False
        with self.assertRaises(RuntimeError):
            await writer.run(self.folder.name, self.data)
        with self.assertRaisesRegex(ValueError, 'incertă'):
            await writer.run(self.folder.name, self.data)
        self.assertEqual(self.created, 1)

    async def test_operation_id_cannot_be_reused_for_other_work(self):
        self.data.dry_run = False
        await writer.run(self.folder.name, self.data)
        self.data.work = 'Altă lucrare'
        with self.assertRaisesRegex(ValueError, 'alte date'):
            await writer.run(self.folder.name, self.data)
        self.assertEqual(self.created, 1)

    async def test_resume_finishes_only_journaled_order_when_another_order_appears(self):
        self.data.dry_run = False
        await writer.run(self.folder.name, self.data)
        async def search(registration):
            return [dict(transaction_id=i, transaction_type='2', status='working') for i in ['999', '1000']]
        with patch.object(writer, '_search_transactions', side_effect=search):
            result = await writer.run(self.folder.name, self.data)
        self.assertEqual(result['id'], '999')
        self.assertEqual(result['other_existing_orders'][0]['id'], '1000')
        self.assertEqual(self.created, 1)


if __name__ == '__main__':
    unittest.main()
