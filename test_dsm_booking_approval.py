import json
import gc
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
import dsm_booking_approval as approval
from dsm_accepted_offers import connection, require_valid_for_booking

VIN = 'WV100000000000008'


def evidence(order_status='working', signature='signed'):
    quote = {'transaction': {'transaction_head__id': 19677, 'transaction_head__code': 'OFERTA-3444',
        'transaction_head__type': '1', 'transaction_head__status': 'completed', 'car_body_series': VIN},
        'sms_or_qrcode_sended': {'service_offer': signature},
        'transaction_head_childs': [{'transaction_id': '19693', 'transaction_head__type': '2', 'transaction_status': '0'}]}
    order = {'transaction': {'transaction_head__id': 19693, 'transaction_head__code': 'PHDEV-10341',
        'transaction_head__type': '2', 'transaction_head__status': order_status, 'car_body_series': VIN,
        'parent_transaction_code': 'OFERTA-3444'}, 'transaction_head_childs': [],
        'transaction_rows': [{'transaction_row__type': 'W', 'transaction_row__qty__line': '2.5'}]}
    return [quote, order]


class DsmBookingApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.db = Path(self.temp.name) / 'test.db'
        with patch.object(database, 'DB_PATH', self.db):
            database.init_db()
        with connection(self.db) as conn:
            conn.execute("INSERT INTO service_requests(id,session_id,vin,registration,source,status,symptom) VALUES (57,'fixture-57',?,?,'whatsapp+dsm-read-only','asteptare','Revizie și constatare')", (VIN, 'B112TST'))
        self.request = dict(id=57, vin=VIN, registration='B112TST', source='whatsapp+dsm-read-only')

    async def refresh(self, payloads):
        refs = [dict(transaction_id=str(p['transaction']['transaction_head__id']),
                     transaction_type=p['transaction']['transaction_head__type']) for p in payloads]
        with patch('dsm_readonly._search_transactions', AsyncMock(return_value=refs)), \
             patch('dsm_open_orders_readonly._evaluate', AsyncMock(return_value=payloads)):
            return await approval.refresh_approval(self.db, self.request)

    async def test_request_57_links_signed_quote_to_active_order_without_local_draft(self):
        record = await self.refresh(evidence())
        self.assertEqual(record['order_code'], 'PHDEV-10341')
        self.assertTrue(approval.valid_approval(record, self.request))
        require_valid_for_booking(self.db, self.request)
        with connection(self.db) as conn:
            self.assertEqual(conn.execute('SELECT source,status,booking_time FROM service_requests').fetchone(),
                             ('whatsapp+dsm-read-only', 'asteptare', 2.5))
        import app
        with patch.object(app, 'DB_PATH', self.db), patch.object(app, '_request_has_approved_offer', return_value=False), \
             patch('dsm_booking_approval.refresh_approval', AsyncMock(return_value=record)), \
             patch.object(app, '_request_by_id', return_value=self.request), \
             patch.object(app, '_confirm_local_dashboard_click'), patch.object(app, '_persist_request_status') as save:
            result = await app.set_request_status(57, app.RequestStatusInput(status='programat'), None, 'update-request-status')
        self.assertEqual(result['status'], 'programat')
        save.assert_called_once_with(57, 'programat')

    async def test_closed_unsigned_wrong_vin_billed_and_unrelated_orders_do_not_qualify(self):
        cases = [evidence(order_status='completed'), evidence(signature='sent')]
        bad = evidence(); bad[1]['transaction']['car_body_series'] = 'WBA00000000000002'; cases.append(bad)
        bad = evidence(); bad[1]['transaction']['parent_transaction_code'] = 'OFERTA-OTHER'; cases.append(bad)
        bad = evidence(); bad[1]['transaction_head_childs'] = [{'transaction_head__type': '4', 'transaction_status': '0'}]; cases.append(bad)
        for payloads in cases:
            with self.subTest(payloads=payloads):
                self.assertEqual(approval.evaluate_candidates(self.request, payloads), [])

    async def test_revoked_order_invalidates_previous_booking_proof(self):
        await self.refresh(evidence())
        self.assertIsNone(await self.refresh(evidence(order_status='completed')))
        with self.assertRaisesRegex(ValueError, 'reverificată'):
            require_valid_for_booking(self.db, self.request)

    async def test_ambiguous_quotes_do_not_pick_an_arbitrary_offer(self):
        second = json.loads(json.dumps(evidence()))
        second[0]['transaction'].update(transaction_head__id=100, transaction_head__code='OFERTA-OTHER')
        second[0]['transaction_head_childs'][0]['transaction_id'] = '101'
        second[1]['transaction'].update(transaction_head__id=101, parent_transaction_code='OFERTA-OTHER')
        with self.assertRaisesRegex(ValueError, 'mai multe'):
            await self.refresh(evidence() + second)
        self.assertIsNone(approval.approval_record(self.db, 57))

    async def test_expired_or_unknown_waiting_quotes_are_not_approved(self):
        for accepted in ('', (datetime.now(timezone.utc)-timedelta(days=11)).isoformat()):
            quote = evidence()[0]
            quote['transaction']['transaction_head__status'] = 'working'
            quote['transaction_head_childs'] = []
            quote['sms_or_qrcode_sended']['service_offer'] = dict(status='signed', signed_at=accepted)
            self.assertEqual(approval.evaluate_candidates(self.request, [quote]), [])

    async def test_cached_proof_cannot_be_reused_for_other_vin_or_after_15_minutes(self):
        record = await self.refresh(evidence())
        self.assertFalse(approval.valid_approval(record, dict(self.request, vin='WBA00000000000002')))
        self.assertFalse(approval.valid_approval(record, self.request, datetime.now(timezone.utc)+timedelta(minutes=16)))


if __name__ == '__main__':
    unittest.main()
