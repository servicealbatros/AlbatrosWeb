import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException
import dsm_accepted_offers as offers
import dsm_acceptance_evidence as evidence


class AcceptanceEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_returns_only_business_evidence_and_never_changes_acceptance_date(self):
        result = dict(sms_or_qrcode_sended={'service_offer': {'status': 'signed'}},
            source_fields=['transaction'], signature_fields=[{'document': 'service_offer', 'type': 'string', 'fields': []}],
            business_dates=[{'field': 'transaction.transaction_head__date', 'value': '2026-11-20'}],
            private_phone='0700100003', private_vin='WBA00000000000002', private_customer='Fixture customer')
        with patch.object(evidence, 'offer_record', return_value={'transaction_id': '1', 'document_code': 'OFERTA-1'}), \
             patch.object(evidence, '_evaluate', AsyncMock(return_value=result)):
            actual = await evidence.inspect('fixture', 80)
        self.assertEqual(actual['signature'], 'signed')
        self.assertEqual(actual['accepted_at'], '')
        self.assertNotIn('private', json.dumps(actual))
        self.assertNotIn('0700100003', json.dumps(actual))
        self.assertNotIn('WBA00000000000002', json.dumps(actual))
        self.assertNotIn('Fixture customer', json.dumps(actual))

    async def test_missing_binding_does_not_contact_dsm(self):
        with patch.object(evidence, 'offer_record', return_value=None), \
             patch.object(evidence, '_evaluate', AsyncMock()) as read:
            with self.assertRaises(ValueError):
                await evidence.inspect('fixture', 80)
            read.assert_not_called()

    def endpoints(self):
        tree = ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8-sig'))
        names = {'scheduler_refresh_signed_offers', '_require_local_vehicle_action', 'dsm_acceptance_evidence'}
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        for node in nodes:
            node.decorator_list = []
        env = dict(Request=object, HTTPException=HTTPException, DB_PATH='fixture', DATA_DIR='fixture',
            _master_access=Mock(), scheduler_accepted_requests=Mock(return_value=[
                {'source': 'dsm_accepted_offer', 'offer_validity': 'unknown'},
                {'source': 'dsm_accepted_offer', 'offer_validity': 'valid'}]))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'app.py', 'exec'), env)
        return env

    async def test_refresh_requires_authenticated_explicit_action_and_does_not_expose_sync_errors(self):
        env = self.endpoints()
        request = SimpleNamespace(state=SimpleNamespace(dashboard_user=None), headers={})
        with patch.object(offers, 'synchronize', AsyncMock(return_value={'ok': True, 'status': 'success',
                'result': {'imported': 1, 'updated': 1}, 'error': 'private DSM data'})) as sync:
            with self.assertRaises(HTTPException):
                await env['scheduler_refresh_signed_offers'](request)
            sync.assert_not_called()
            request.state.dashboard_user = 'consilier'
            request.headers['X-Human-Action'] = 'refresh-signed-dsm-offers'
            result = await env['scheduler_refresh_signed_offers'](request)
        self.assertEqual((result['imported'], result['ready'], result['missing_acceptance_date']), (1, 1, 1))
        self.assertNotIn('private', json.dumps(result))

    async def test_master_evidence_authentication_and_failure_are_safe(self):
        env = self.endpoints()
        with patch.object(evidence, 'inspect', AsyncMock(side_effect=ValueError('private DSM data'))):
            with self.assertRaises(HTTPException) as error:
                await env['dsm_acceptance_evidence'](80, object())
            self.assertEqual(error.exception.status_code, 502)
            self.assertNotIn('private', error.exception.detail)
        env['_master_access'].assert_called_once()


if __name__ == '__main__':
    unittest.main()
