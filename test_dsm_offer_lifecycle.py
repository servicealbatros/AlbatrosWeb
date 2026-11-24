import json
import gc
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import database
import dsm_offer_lifecycle as lifecycle
import dsm_booking_reconciliation as bookings
from dsm_accepted_offers import apply_snapshot, connection, offer_record, dashboard_dates, require_valid_for_booking, state, save_state, synchronize
from dsm_billing_sync import reconcile_billed_requests
from scheduler import init_scheduler_db, accepted_requests, request_by_id
from test_dsm_accepted_offers import item


def document(ident='1', kind='1', code='OFERTA-1', status='working', signature='signed', **extra):
    return dict(transaction=dict(transaction_head__id=ident, transaction_head__type=kind,
        transaction_head__code=code, transaction_head__status=status, car_id='25',
        car_body_series='WBA00000000000002', car_auto_number='B100TST', **extra),
        transaction_head_childs=[], sms_or_qrcode_sended={'service_offer': signature})


class OfferLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.db = Path(self.temp.name) / 'test.db'
        with patch.object(database, 'DB_PATH', self.db):
            database.init_db()
        init_scheduler_db(self.db)
        self.quote = document(status='completed')
        self.order = document('2', '2', 'PHDEV-10', signature='', parent_transaction_type='1', parent_transaction_code='OFERTA-1')

    def converted_item(self, quote=None, order=None):
        quote = quote or self.quote
        order = order or self.order
        row = next(r for r in lifecycle.classify({'1': quote, '2': order}) if r['id'] == '1')
        return {**row, 'detail': {**item()['detail'], **row['extra']}}

    def import_converted(self):
        return apply_snapshot(self.db, [self.converted_item()])['request_ids'][0]

    def test_signed_conversion_keeps_same_dashboard_card_and_waiting_work(self):
        ident = apply_snapshot(self.db, [item()])['request_ids'][0]
        result = apply_snapshot(self.db, [self.converted_item()])
        self.assertEqual(result['request_ids'], [ident])
        self.assertEqual(result['converted_waiting'], 1)
        self.assertEqual(request_by_id(self.db, ident)['status'], 'acceptat')
        waiting = accepted_requests(self.db)
        self.assertEqual(len(waiting), 1)
        self.assertTrue(waiting[0]['dsm_signature_confirmed'])
        self.assertTrue(waiting[0]['dsm_has_open_order'])
        self.assertEqual(waiting[0]['dsm_order_codes'], ['PHDEV-10'])
        self.assertEqual(waiting[0]['offer_validity'], 'unknown')

    def test_signed_quote_and_order_are_one_work_not_two_cards(self):
        self.order['sms_or_qrcode_sended']['service_offer'] = 'signed'
        rows = lifecycle.classify({'1': self.quote, '2': self.order})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], '1')

    def test_parent_discovered_later_preserves_existing_card(self):
        self.order['sms_or_qrcode_sended']['service_offer'] = 'signed'
        row = lifecycle.classify({'2': self.order})[0]
        row['detail'] = {**item('2','PHDEV-10')['detail'], **row['extra']}
        ident = apply_snapshot(self.db, [row])['request_ids'][0]
        self.assertEqual(apply_snapshot(self.db, [self.converted_item()])['request_ids'], [ident])
        self.assertEqual(len(accepted_requests(self.db)), 1)
        self.assertEqual(offer_record(self.db, ident)['transaction_id'], '1')

    def test_unsigned_open_order_does_not_imply_signed_offer(self):
        self.quote['sms_or_qrcode_sended'] = {}
        apply_snapshot(self.db, [self.converted_item()])
        self.assertEqual(accepted_requests(self.db), [])
        with connection(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0], 0)

    def review_item(self):
        self.quote['sms_or_qrcode_sended'] = {}
        return self.converted_item()

    def test_review_order_is_visible_without_being_accepted_or_bookable(self):
        data = self.review_item()
        ident = apply_snapshot(self.db, [data], include_unconfirmed=True)['request_ids'][0]
        waiting = accepted_requests(self.db)
        self.assertEqual(len(waiting), 1)
        row = waiting[0]
        self.assertEqual(row['id'], ident)
        self.assertEqual(row['status'], 'ofertat')
        self.assertTrue(row['dsm_requires_review'])
        self.assertTrue(row['dsm_has_open_order'])
        self.assertFalse(row['dsm_signature_confirmed'])
        self.assertEqual(row['dsm_lifecycle_label'], 'De verificat · deviz deschis: PHDEV-10')
        with self.assertRaises(ValueError):
            require_valid_for_booking(self.db, row)
        apply_snapshot(self.db, [data], include_unconfirmed=True)
        self.assertEqual([r['id'] for r in accepted_requests(self.db)], [ident])
        self.quote['sms_or_qrcode_sended'] = {'service_offer': 'signed'}
        apply_snapshot(self.db, [self.converted_item()], include_unconfirmed=True)
        row = accepted_requests(self.db)[0]
        self.assertEqual(row['id'], ident)
        self.assertEqual(row['status'], 'acceptat')
        self.assertFalse(row['dsm_requires_review'])
        self.assertTrue(row['dsm_signature_confirmed'])

    def test_closed_cancelled_archived_and_already_booked_review_orders_stay_out(self):
        from scheduler import cancel_waiting_request
        data = self.review_item()
        ident = apply_snapshot(self.db, [data], include_unconfirmed=True)['request_ids'][0]
        cancel_waiting_request(self.db, ident)
        apply_snapshot(self.db, [data], include_unconfirmed=True)
        self.assertEqual(accepted_requests(self.db), [])
        with connection(self.db) as conn:
            conn.execute("UPDATE service_requests SET status='ofertat' WHERE id=?", (ident,))
            conn.execute('CREATE TABLE request_archive (request_id INTEGER PRIMARY KEY)')
            conn.execute('INSERT INTO request_archive VALUES (?)', (ident,))
        self.assertEqual(accepted_requests(self.db), [])
        with connection(self.db) as conn:
            conn.execute('DELETE FROM request_archive')
            conn.execute("INSERT INTO scheduler_appointments (request_id,start_at,end_at,estimated_hours,operations) VALUES (?,'2026-11-24T08:30','2026-11-24T09:30',1,'Test work')", (ident,))
        self.assertEqual(accepted_requests(self.db), [])
        with connection(self.db) as conn:
            conn.execute('DELETE FROM scheduler_appointments')
        for reason in ['closed', 'unsigned']:
            apply_snapshot(self.db, [{**data, 'reason': reason}], include_unconfirmed=True)
            self.assertEqual(accepted_requests(self.db), [])

    def test_review_queue_does_not_hide_waiting_work_after_forty_cards(self):
        data = self.review_item()
        rows = []
        for i in range(1,46):
            row = deepcopy(data)
            row.update(id=str(i), code='PHDEV-'+str(i))
            row['detail'].update(transaction_id=str(i), document_code=row['code'], document_type='2', order_ids=[str(i)], order_codes=[row['code']])
            rows.append(row)
        ids = apply_snapshot(self.db, rows, include_unconfirmed=True)['request_ids']
        self.assertEqual({r['id'] for r in accepted_requests(self.db)}, set(ids))

    async def test_hourly_sync_enables_review_orders_and_persists_setting(self):
        self.assertTrue(state(self.temp.name)['include_open_orders_for_review'])
        with patch('dsm_accepted_offers.read_snapshot', AsyncMock(return_value=[self.review_item()])), \
             patch('dsm_billing_sync.reconcile_billed_requests', AsyncMock(return_value={})):
            result = await synchronize(self.db, self.temp.name)
        self.assertTrue(result['ok'])
        self.assertTrue(accepted_requests(self.db)[0]['dsm_requires_review'])
        info = state(self.temp.name)
        self.assertEqual(info['interval_hours'], 1)
        info['include_open_orders_for_review'] = False
        save_state(self.temp.name, info)
        self.assertFalse(state(self.temp.name)['include_open_orders_for_review'])

    def test_previous_verified_signature_survives_conversion_but_not_explicit_revocation(self):
        ident = apply_snapshot(self.db, [item()])['request_ids'][0]
        self.quote['sms_or_qrcode_sended'] = {}
        apply_snapshot(self.db, [self.converted_item()])
        self.assertEqual(request_by_id(self.db, ident)['status'], 'acceptat')
        self.assertEqual(json.loads(offer_record(self.db, ident)['payload_json'])['signature_basis'],
                         'previously_verified_offer_and_exact_open_order')
        self.quote['sms_or_qrcode_sended'] = {'service_offer': 'update'}
        apply_snapshot(self.db, [self.converted_item()])
        self.assertEqual(request_by_id(self.db, ident)['status'], 'ofertat')
        self.assertEqual(accepted_requests(self.db), [])

    def test_changed_vehicle_cannot_inherit_previous_signature(self):
        apply_snapshot(self.db, [item()])
        self.quote['sms_or_qrcode_sended'] = {}
        row = self.converted_item()
        row['detail']['vin'] = 'WBA00000000000003'
        apply_snapshot(self.db, [row])
        self.assertEqual(accepted_requests(self.db), [])

    def test_same_plate_other_work_closed_or_cancelled_order_never_qualifies(self):
        for key, value in [('car_body_series', 'WBA00000000000003'), ('car_id', '26'),
                           ('parent_transaction_code', 'OFERTA-2'), ('transaction_head__status', 'billed'),
                           ('transaction_head__status', 'canceled')]:
            order = deepcopy(self.order)
            order['transaction'][key] = value
            rows = lifecycle.classify({'1': self.quote, '2': order})
            self.assertFalse(any(r['id'] == '1' and r['extra'].get('order_ids') for r in rows), (key,value))

    def test_only_native_sent_state_is_imported_as_offered(self):
        for state in ['sent', 'delivered', 'pending_signature', 'update', 'insert', '']:
            row = lifecycle.classify({'1': document(signature=state)})[0]
            self.assertEqual(row['reason'] == 'offered', state in lifecycle.SENT)
        row = item(); row.update(reason='offered', signature='sent')
        row['detail']['queue_stage'] = 'sent_offer'
        ident = apply_snapshot(self.db, [row])['request_ids'][0]
        self.assertEqual(request_by_id(self.db, ident)['status'], 'ofertat')
        self.assertFalse(dashboard_dates(offer_record(self.db, ident))['dsm_signature_confirmed'])
        self.assertEqual(accepted_requests(self.db), [])

    async def test_reader_discovers_closed_parent_and_uses_current_order_operations(self):
        page = dict(rows=[dict(transaction_id='1', transaction_type='1', code='OFERTA-1', status='completed'),
                          dict(transaction_id='2', transaction_type='2', code='PHDEV-10', status='working')], limit=100)
        async def roots(ids):
            return {i: {'1': self.quote, '2': self.order}[i] for i in ids}
        async def details(expression):
            return [dict(detail={**item(i)['detail'], 'description': 'Current order work' if i == '2' else 'Old quote work'}) for i in ['1','2']]
        with patch('dsm_open_orders_readonly._listing_page', AsyncMock(return_value=page)), \
             patch.object(bookings, 'read_offer_details', AsyncMock(side_effect=roots)) as read, \
             patch('dsm_open_orders_readonly._evaluate', AsyncMock(side_effect=details)):
            rows = await lifecycle.read_snapshot([])
        self.assertEqual(read.await_args_list[0].args[0], ['2'])
        self.assertEqual(read.await_args_list[1].args[0], ['1'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['detail']['description'], 'Current order work')
        self.assertEqual(rows[0]['detail']['transaction_id'], '1')
        self.assertEqual(rows[0]['detail']['document_code'], 'OFERTA-1')

    async def test_exact_open_order_is_rechecked_before_booking(self):
        ident = self.import_converted()
        request = request_by_id(self.db, ident)
        with self.assertRaises(ValueError):
            require_valid_for_booking(self.db, request)
        with patch.object(bookings, 'read_offer_details', AsyncMock(return_value={'1': self.quote, '2': self.order})):
            proof = await lifecycle.refresh_order_approval(self.db, request, offer_record(self.db, ident))
        self.assertEqual(proof['order_id'], '2')
        require_valid_for_booking(self.db, request)
        for key, value in [('transaction_head__status','billed'), ('car_id','26'), ('parent_transaction_code','OFERTA-2')]:
            changed = deepcopy(self.order); changed['transaction'][key] = value
            with patch.object(bookings, 'read_offer_details', AsyncMock(return_value={'1': self.quote,'2': changed})):
                with self.assertRaises(ValueError):
                    await lifecycle.refresh_order_approval(self.db, request, offer_record(self.db, ident))

    async def test_signed_order_without_quote_can_wait_and_close_from_exact_billing(self):
        self.order['sms_or_qrcode_sended']['service_offer'] = 'signed'
        row = lifecycle.classify({'2': self.order})[0]
        row['detail'] = {**item('2','PHDEV-10')['detail'], **row['extra']}
        ident = apply_snapshot(self.db, [row])['request_ids'][0]
        self.assertEqual(accepted_requests(self.db)[0]['id'], ident)
        with patch('dsm_billing_sync._read_details', AsyncMock(return_value={'2': self.order})):
            self.assertEqual((await reconcile_billed_requests(self.db))['resolved_request_ids'], [])
        self.order['transaction']['transaction_head__status'] = 'billed'
        with patch('dsm_billing_sync._read_details', AsyncMock(return_value={'2': self.order})):
            self.assertEqual((await reconcile_billed_requests(self.db))['resolved_request_ids'], [ident])

    async def test_same_car_booking_is_not_enough_but_exact_native_order_link_resolves(self):
        ident = self.import_converted()
        detail = dict(code='APP-2215', carId='25', carNumber='B100TST', description='Other work',
                      start='2026-11-23T08:30', end='2026-11-23T09:30', canCancel=True)
        with patch.object(bookings, 'read_offer_details', AsyncMock(return_value={'1': self.quote,'2': self.order})), \
             patch.object(bookings, 'search_appointments', AsyncMock(return_value={'20391'})), \
             patch.object(bookings, 'read_appointment', AsyncMock(return_value=detail)):
            self.assertEqual((await bookings.reconcile(self.db, apply=True))['resolved_request_ids'], [])
            detail.update(description='OFERTA-1', estimateType='2', estimateId='99')
            self.assertEqual((await bookings.reconcile(self.db, apply=True))['resolved_request_ids'], [])
            detail.update(estimateType='2', estimateId='2')
            result = await bookings.reconcile(self.db, apply=True)
        self.assertEqual(result['resolved_request_ids'], [ident])
        self.assertEqual(result['verified'][0]['dsm_appointment_id'], 'APP-2215')
        self.assertEqual(result['verified'][0]['dsm_native_appointment_id'], '20391')

    def test_unrelated_payloads_cannot_supply_native_booking_relation(self):
        row = dict(transaction_id='1', registration='B100TST', id=1, order_ids=['2'])
        unrelated = document('9')
        unrelated['transaction_head_childs'] = [dict(transaction_id='20391', transaction_head__type='3')]
        self.assertEqual(bookings.appointment_ids(row, [], self.quote, {'9': unrelated,'2':self.order}), set())


if __name__ == '__main__':
    unittest.main()
