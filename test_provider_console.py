import os
import time
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock
from types import SimpleNamespace
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

    def test_captcha_destination_is_limited_to_verified_recaptcha_frames(self):
        self.assertTrue(console.captcha_url('https://www.google.com/recaptcha/api2/anchor?k=example'))
        self.assertTrue(console.captcha_url('https://www.recaptcha.net/recaptcha/api2/bframe'))
        for url in ('https://google.com.attacker.example/recaptcha/api2/anchor',
                    'http://www.google.com/recaptcha/api2/anchor',
                    'https://www.google.com/search', 'https://user:pass@www.google.com/recaptcha/'):
            self.assertFalse(console.captcha_url(url))

    def test_captcha_click_requires_session_origin_and_current_frame_region(self):
        endpoint = '/api/provider-auth/captcha-click'
        self.assertEqual(self.client.post(endpoint, json={'x': 12, 'y': 15}).status_code, 403)
        console.SESSIONS['human-test'] = time.time() + 60
        self.client.cookies.set('albatros_provider_test', 'human-test')
        self.assertEqual(self.client.post(endpoint, json={'x': 12, 'y': 15},
            headers={'Origin': 'https://other.example'}).status_code, 403)
        with patch.object(console, 'captcha_regions', AsyncMock(return_value=[
                {'x': 20, 'y': 20, 'width': 100, 'height': 100}])):
            self.assertEqual(self.client.post(endpoint, json={'x': 12, 'y': 15},
                headers={'Origin': 'https://test.example'}).status_code, 403)
        self.assertFalse(console.inside_region(120, 30, [{'x': 20, 'y': 20, 'width': 100, 'height': 100}]))
        self.assertTrue(console.inside_region(30, 30, [{'x': 20, 'y': 20, 'width': 100, 'height': 100}]))

    def test_human_captcha_click_is_forwarded_only_inside_frame(self):
        console.SESSIONS['human-test'] = time.time() + 60
        self.client.cookies.set('albatros_provider_test', 'human-test')
        mouse = SimpleNamespace(click=AsyncMock())
        with patch.object(console, '_page', SimpleNamespace(mouse=mouse)), \
             patch.object(console, 'captcha_regions', AsyncMock(return_value=[
                 {'x': 20, 'y': 20, 'width': 100, 'height': 100}])), \
             patch.object(console, 'snapshot', AsyncMock(return_value={'page_open': True})), \
             patch.object(console.asyncio, 'sleep', AsyncMock()):
            response = self.client.post('/api/provider-auth/captcha-click', json={'x': 30, 'y': 30},
                headers={'Origin': 'https://test.example'})
            self.assertEqual(response.status_code, 200)
            mouse.click.assert_awaited_once_with(30, 30)


if __name__ == '__main__':
    unittest.main()
