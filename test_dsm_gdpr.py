import copy
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

import dsm_gdpr as gdpr
import pre_reception as flow
import pre_arrival_reminders as reminders
from test_pre_arrival_policy import data, head, PHONE

LINK = 'https://apps.dsmpro.ro/sign/example-test-only'


def case():
    result = data('2024-01-01')
    result['document_source'] = 'document'
    result['previous_dsm'].update(client_name=result['client_name'], client_phone=PHONE,
                                  client_address_street=result['client_address'])
    return result


def payload(**changes):
    result = dict(transaction=head('10', status='working'), electronic={'has_qrcode': '1'},
                  sms_or_qrcode_sended={})
    result.update(changes)
    return result


class NativeAgreementTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_once_persist_before_request_and_reuse_link(self):
        value = case()
        states = []
        checkpoint = lambda: states.append(copy.deepcopy(value.get('gdpr')))

        async def generate(ident):
            self.assertEqual(ident, '10')
            self.assertEqual(states[-1]['state'], 'generating')
            return dict(status='1', link=LINK)

        with patch.object(gdpr, 'call', AsyncMock(return_value=payload())), \
                patch.object(gdpr, 'generate', AsyncMock(side_effect=generate)) as create:
            for _ in range(3):
                result = await gdpr.prepare(value, checkpoint)
                self.assertEqual(result['state'], 'pending')
                self.assertEqual(result['link'], LINK)
            create.assert_awaited_once()

    async def test_native_appointment_id_is_never_used_as_transaction_id(self):
        value = case(); value.update(source_type='3', source_transaction_id='694')
        with patch.object(gdpr, 'call', AsyncMock()) as read, patch.object(gdpr, 'generate', AsyncMock()) as create:
            result = await gdpr.prepare(value, Mock())
            self.assertEqual(result['state'], 'awaiting_order')
            read.assert_not_awaited(); create.assert_not_awaited()

    async def test_after_order_uses_real_verified_order_not_native_appointment(self):
        value = case(); value.update(source_type='3', source_transaction_id='694', order={'id': '10', 'code': 'PHDEV-10'})
        with patch.object(gdpr, 'call', AsyncMock(return_value=payload())) as read, \
                patch.object(gdpr, 'generate', AsyncMock(return_value=dict(status='1', link=LINK))):
            self.assertEqual((await gdpr.prepare(value, Mock()))['state'], 'pending')
            read.assert_awaited_once_with('/special/transaction/get_transaction/10')

    async def test_customer_vehicle_type_and_id_isolation_before_generation(self):
        for key, invalid in [('contact_id', '44'), ('car_id', '44'), ('car_body_series', 'WBA00000000000002'),
                             ('transaction_head__type', '3'), ('transaction_head__id', '11')]:
            with self.subTest(key=key):
                response = payload(); response['transaction'][key] = invalid
                with patch.object(gdpr, 'call', AsyncMock(return_value=response)), \
                        patch.object(gdpr, 'generate', AsyncMock()) as create:
                    with self.assertRaises(ValueError):
                        await gdpr.prepare(case(), Mock())
                    create.assert_not_awaited()

    async def test_missing_vin_never_generates_a_document_for_generic_car(self):
        value = case(); value['vin'] = ''
        response = payload(); response['transaction']['car_body_series'] = ''
        with patch.object(gdpr, 'call', AsyncMock(return_value=response)), patch.object(gdpr, 'generate', AsyncMock()) as create:
            with self.assertRaises(ValueError):
                await gdpr.prepare(value, Mock())
            create.assert_not_awaited()

    async def test_timeout_negative_reply_and_unsafe_link_never_retry_generation(self):
        for result in [TimeoutError(), dict(status='0'), dict(status='1', link='https://attacker.invalid/sign'),
                       dict(status='1', qrcode='image-only-no-link')]:
            value = case()
            generator = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
            with patch.object(gdpr, 'call', AsyncMock(return_value=payload())), patch.object(gdpr, 'generate', generator):
                for _ in range(2):
                    self.assertEqual((await gdpr.prepare(value, Mock()))['state'], 'review')
                generator.assert_awaited_once()

    async def test_restart_after_generation_claim_does_not_repeat_post(self):
        value = case()
        value['gdpr'] = dict(state='generating', binding=gdpr.identity(value), transaction_id='10', transaction_type='2')
        with patch.object(gdpr, 'call', AsyncMock(return_value=payload())), patch.object(gdpr, 'generate', AsyncMock()) as create:
            self.assertEqual((await gdpr.prepare(value, Mock()))['reason'], 'generation_uncertain')
            create.assert_not_awaited()

    async def test_only_actual_gdpr_document_state_proves_remote_signature(self):
        for state in ['sent', 'pending', 'failed', {'status': 'pending'}]:
            response = payload(sms_or_qrcode_sended={'2_statement_of_agreement': state,
                                                     '2_service_offer': 'signed'})
            response['electronic']['has_gdpr'] = '1'  # Uploaded paper alone is not proof of remote signing.
            with patch.object(gdpr, 'call', AsyncMock(return_value=response)), patch.object(gdpr, 'generate', AsyncMock()) as create:
                self.assertNotEqual((await gdpr.prepare(case(), Mock(), check_only=True))['state'], 'signed')
                create.assert_not_awaited()
        for state in ['signed', 'completed', {'record_status__code': 'complete'}]:
            with patch.object(gdpr, 'call', AsyncMock(return_value=payload(sms_or_qrcode_sended={'2_statement_of_agreement': state}))), \
                    patch.object(gdpr, 'generate', AsyncMock()) as create:
                result = await gdpr.prepare(case(), Mock(), check_only=True)
                self.assertEqual(result['state'], 'signed')
                self.assertTrue(result['verified_at']); self.assertNotIn('link', result)
                create.assert_not_awaited()

    async def test_read_only_check_can_recover_uncertain_generation_without_new_post(self):
        value = case()
        value['gdpr'] = dict(state='review', reason='generation_uncertain', binding=gdpr.identity(value),
                             transaction_id='10', transaction_type='2')
        with patch.object(gdpr, 'call', AsyncMock(return_value=payload(sms_or_qrcode_sended={'2_statement_of_agreement': 'signed'}))), \
                patch.object(gdpr, 'generate', AsyncMock()) as create:
            self.assertEqual((await gdpr.prepare(value, Mock(), check_only=True))['state'], 'signed')
            create.assert_not_awaited()

    async def test_changed_customer_data_does_not_reuse_or_regenerate_signature(self):
        value = case()
        value['gdpr'] = dict(state='signed', binding=gdpr.identity(value), transaction_id='10', transaction_type='2')
        value['client_name'] = 'Different Test Client'
        with patch.object(gdpr, 'call', AsyncMock()) as read, patch.object(gdpr, 'generate', AsyncMock()) as create:
            self.assertEqual((await gdpr.prepare(value, Mock()))['reason'], 'client_data_changed')
            read.assert_not_awaited(); create.assert_not_awaited()

    async def test_closed_documents_and_disabled_native_signing_do_not_generate(self):
        for response in [payload(electronic={'has_qrcode': '0'}), payload()]:
            if response.get('electronic', {}).get('has_qrcode') != '0':
                response['transaction']['transaction_head__status'] = 'billed'
            with patch.object(gdpr, 'call', AsyncMock(return_value=response)), patch.object(gdpr, 'generate', AsyncMock()) as create:
                self.assertEqual((await gdpr.prepare(case(), Mock()))['state'], 'review')
                create.assert_not_awaited()

    async def test_explicit_existing_order_and_offer_type_are_validated_separately(self):
        for kind in ['1', '2', '3']:
            value = case(); value.update(source_type=kind, source_transaction_id='694')
            response = payload()
            if kind == '3':
                value['approved_existing_order_id'] = '10'
                expected = '2'
            else:
                response['transaction']['transaction_head__id'] = '694'
                response['transaction']['transaction_head__type'] = kind
                expected = kind
            response['sms_or_qrcode_sended'] = {expected + '_statement_of_agreement': 'signed'}
            with patch.object(gdpr, 'call', AsyncMock(return_value=response)) as read, \
                    patch.object(gdpr, 'generate', AsyncMock()) as create:
                result = await gdpr.prepare(value, Mock())
                self.assertEqual(result['state'], 'signed')
                self.assertEqual(result['transaction_type'], expected)
                read.assert_awaited_once_with('/special/transaction/get_transaction/' + ('10' if kind == '3' else '694'))
                create.assert_not_awaited()

    async def test_lost_signature_is_not_retained_and_unrelated_document_not_accepted(self):
        value = case()
        value['gdpr'] = dict(state='signed', binding=gdpr.identity(value), transaction_id='10', transaction_type='2')
        response = payload(sms_or_qrcode_sended={'1_statement_of_agreement': 'signed', '2_service_offer': 'signed'})
        with patch.object(gdpr, 'call', AsyncMock(return_value=response)), patch.object(gdpr, 'generate', AsyncMock()) as create:
            self.assertEqual((await gdpr.prepare(value, Mock(), check_only=True))['reason'], 'signature_no_longer_confirmed')
            create.assert_not_awaited()

    async def test_non_production_and_no_photo_path_have_no_supplier_effect(self):
        for key, val in [('production', False), ('document_source', 'dsm')]:
            value = case(); value[key] = val
            with patch.object(gdpr, 'call', AsyncMock()) as read:
                self.assertEqual(await gdpr.prepare(value, Mock()), {})
                read.assert_not_awaited()

    async def test_transport_only_calls_observed_native_route_never_sms_or_signature(self):
        with patch.object(gdpr, '_evaluate', AsyncMock(return_value={})) as evaluate:
            await gdpr.generate('10')
            script = evaluate.await_args.args[0]
            self.assertIn('/special/transaction/generate_qr_code/10/statement_of_agreement', script)
            self.assertIn("method:'POST'", script)
            self.assertIn('X-CSRF-TOKEN', script)
            self.assertNotIn('send_sms_transaction', script)
            self.assertNotIn('start_transaction', script)
            with self.assertRaises(ValueError):
                await gdpr.generate('../10')

    def test_foreign_plain_http_credentials_pdf_ports_and_newline_links_rejected(self):
        for value in ['https://dsmpro.ro.attacker.invalid/sign', 'http://apps.dsmpro.ro/sign',
                      'https://evil@payload.dsmpro.ro/sign', 'https://apps.dsmpro.ro/file.pdf',
                      'https://apps.dsmpro.ro:8000/sign', 'https://evil.invalid\\apps.dsmpro.ro/sign',
                      LINK+'\ntext', '/local/sign', None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                gdpr.signing_link(value)
        self.assertEqual(gdpr.signing_link(LINK), LINK)


class AgreementFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.send = AsyncMock()
        self.writer = AsyncMock(return_value={'id': '10', 'code': 'PHDEV-10', 'itp': '2030-01-01'})

    async def handle(self, body, value=None, stage=None):
        if value is not None:
            flow.save(self.root, PHONE, stage or 'attendance', value)
        return await flow.text(self.root, PHONE, body, self.send, self.writer, agreement=gdpr.prepare)

    async def test_link_in_same_message_as_talon_deduplicated_after_restart(self):
        with patch.object(gdpr, 'call', AsyncMock(return_value=payload())), \
                patch.object(gdpr, 'generate', AsyncMock(return_value=dict(status='1', link=LINK))) as create:
            await self.handle('DA', case())
            self.assertIn('fotografie clară', self.send.await_args.args[1])
            self.assertIn(LINK, self.send.await_args.args[1])
            self.assertEqual(flow.get(self.root, PHONE)[1]['gdpr']['delivery'], 'submitted')
            value = flow.get(self.root, PHONE)[1]
            await flow.request_document(self.root, PHONE, value, self.send, agreement=gdpr.prepare)
            self.assertNotIn(LINK, self.send.await_args.args[1])
            create.assert_awaited_once()

    async def test_first_visit_defers_link_until_confirmed_order_without_premature_writer(self):
        value = case(); value.update(source_type='3', source_transaction_id='694')
        with patch.object(gdpr, 'call', AsyncMock(return_value=payload())), \
                patch.object(gdpr, 'generate', AsyncMock(return_value=dict(status='1', link=LINK))) as create:
            await self.handle('DA', value)
            self.writer.assert_not_awaited(); create.assert_not_awaited()
            self.assertIn('după confirmarea datelor', self.send.await_args.args[1])
            value = flow.get(self.root, PHONE)[1]
            value.update(document_verified=True, document_current=True, document_fields_confirmed=True,
                         itp='2030-01-01', owner_name='Client Test', manufacture_year=2020, phone_confirmed=True, extra='Nu')
            await self.handle('CONFIRM DATELE', value, 'confirm')
            self.writer.assert_awaited_once()
            self.assertIn(LINK, self.send.await_args.args[1])
            self.assertEqual(flow.get(self.root, PHONE)[0], 'done')
            create.assert_awaited_once()
            await self.handle('CONFIRM DATELE')
            self.writer.assert_awaited_once(); create.assert_awaited_once()

    async def test_whatsapp_claim_does_not_replace_native_signature_and_keeps_photo_stage(self):
        with patch.object(gdpr, 'call', AsyncMock(return_value=payload())) as read, \
                patch.object(gdpr, 'generate', AsyncMock(return_value=dict(status='1', link=LINK))):
            await self.handle('DA', case())
            await self.handle('AM SEMNAT')
            self.assertIn('nu este încă verificată', self.send.await_args.args[1])
            read.return_value = payload(sms_or_qrcode_sended={'2_statement_of_agreement': 'signed'})
            await self.handle('AM SEMNAT')
            self.assertIn('este confirmată în DSM', self.send.await_args.args[1])
            self.assertIn('Așteptăm fotografiile', self.send.await_args.args[1])
            stage, saved = flow.get(self.root, PHONE)
            self.assertEqual(stage, 'document'); self.assertEqual(saved['gdpr']['state'], 'signed')
            self.writer.assert_not_awaited()

    async def test_uncertain_send_does_not_automatically_resend_link(self):
        value = case()
        with patch.object(gdpr, 'call', AsyncMock(return_value=payload())), \
                patch.object(gdpr, 'generate', AsyncMock(return_value=dict(status='1', link=LINK))) as create:
            self.send.side_effect = TimeoutError()
            with self.assertRaises(TimeoutError):
                await flow.request_document(self.root, PHONE, value, self.send, agreement=gdpr.prepare)
            self.assertEqual(flow.get(self.root, PHONE)[1]['gdpr']['delivery'], 'uncertain')
            self.send.side_effect = None
            await flow.request_document(self.root, PHONE, flow.get(self.root, PHONE)[1], self.send, agreement=gdpr.prepare)
            self.assertNotIn(LINK, self.send.await_args.args[1]); create.assert_awaited_once()

    async def test_recent_dsm_reuse_path_never_requests_gdpr_or_photos(self):
        value = data(date.today().isoformat())
        agreement = AsyncMock()
        flow.save(self.root, PHONE, 'attendance', value)
        await flow.text(self.root, PHONE, 'DA', self.send, self.writer, agreement=agreement)
        agreement.assert_not_awaited()
        self.assertNotIn('GDPR', self.send.await_args.args[1])

    async def test_done_order_can_verify_signature_without_creating_another_order(self):
        value = case(); value['order'] = {'id': '10', 'code': 'PHDEV-10'}
        with patch.object(gdpr, 'call', AsyncMock(return_value=payload(sms_or_qrcode_sended={'2_statement_of_agreement': 'signed'}))):
            await self.handle('AM SEMNAT', value, 'done')
            self.assertEqual(flow.get(self.root, PHONE)[1]['gdpr']['state'], 'signed')
            self.writer.assert_not_awaited()

    async def test_stopped_recipient_never_triggers_native_checks_or_messages(self):
        with patch.object(gdpr, 'call', AsyncMock()) as read:
            self.assertFalse(await self.handle('AM SEMNAT', case(), 'stopped'))
            read.assert_not_awaited(); self.send.assert_not_awaited(); self.writer.assert_not_awaited()

    async def test_dsm_outage_keeps_signature_unverified_and_exposes_review(self):
        with patch.object(gdpr, 'call', AsyncMock(side_effect=TimeoutError())):
            await self.handle('AM SEMNAT', case(), 'done')
            self.assertEqual(flow.get(self.root, PHONE)[1]['gdpr']['state'], 'review')
            self.assertIn('nu este încă verificată', self.send.await_args.args[1])
            self.writer.assert_not_awaited()

    async def test_adviser_status_has_separate_signature_and_delivery_without_private_links(self):
        value = case(); value['gdpr'] = dict(state='pending', delivery='uncertain', link=LINK)
        flow.save(self.root, PHONE, 'done', value)
        report = reminders.status(self.root)
        self.assertEqual(report['cases'][0]['gdpr_state'], 'pending')
        self.assertEqual(report['cases'][0]['gdpr_delivery'], 'uncertain')
        self.assertEqual(report['issues'][0]['state'], 'review')
        self.assertNotIn(LINK, str(report)); self.assertNotIn(PHONE, str(report))


if __name__ == '__main__':
    unittest.main()
