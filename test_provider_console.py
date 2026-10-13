import os
import time
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
import provider_test_console as console


class ConsoleAccessTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'ALBATROS_PROVIDER_TEST_CONSOLE': 'true', 'ADMIN_API_TOKEN': 'console-test-key'})
        self.env.start()
        console.GRANTS.clear()
        console.SESSIONS.clear()
        app = FastAPI()
        app.include_router(console.router)
        self.client = TestClient(app, base_url='https://test.example')

    def tearDown(self):
        self.env.stop()

    def test_unauthenticated_browser_access_is_denied(self):
        self.assertEqual(self.client.get('/api/provider-auth/state').status_code, 403)
        self.assertEqual(self.client.post('/api/admin/provider-test-grant').status_code, 403)

    def test_disabled_console_cannot_be_unlocked_by_admin(self):
        with patch.dict(os.environ, {'ALBATROS_PROVIDER_TEST_CONSOLE': 'false'}):
            self.assertEqual(self.client.post('/api/admin/provider-test-grant',
                headers={'Authorization': 'Bearer console-test-key'}).status_code, 404)

    def test_grant_is_single_use_and_scoped_cookie_is_secure(self):
        response = self.client.post('/api/admin/provider-test-grant',
            headers={'Authorization': 'Bearer console-test-key'})
        token = response.json()['url'].split('#')[1]
        response = self.client.post('/api/provider-auth/session', json={'token': token},
                                   headers={'Origin': 'https://test.example'})
        self.assertEqual(response.status_code, 200)
        cookie = response.headers['set-cookie'].lower()
        self.assertIn('httponly', cookie)
        self.assertIn('secure', cookie)
        self.assertIn('samesite=strict', cookie)
        self.assertEqual(self.client.post('/api/provider-auth/session', json={'token': token},
                          headers={'Origin': 'https://test.example'}).status_code, 403)

    def test_cross_origin_or_expired_grant_is_rejected(self):
        console.GRANTS['test-grant'] = time.time() - 1
        self.assertEqual(self.client.post('/api/provider-auth/session', json={'token': 'test-grant'},
            headers={'Origin': 'https://other.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/provider-auth/session', json={'token': 'test-grant'},
            headers={'Origin': 'https://test.example'}).status_code, 403)

    def test_destination_validation_rejects_spoofed_domains(self):
        self.assertTrue(console.allowed_url('https://ro.e-cat.intercars.eu/'))
        for url in ('https://intercars.eu.attacker.example/', 'http://apps.dsmpro.ro/',
                    'https://user:pass@apps.dsmpro.ro/', 'file:///tmp/secret'):
            self.assertFalse(console.allowed_url(url))


if __name__ == '__main__':
    unittest.main()
