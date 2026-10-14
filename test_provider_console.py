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
        self.page_key = patch.object(console, '_page_id', 'test-page')
        self.page_key.start()
        console.GRANTS.clear()
        console.SESSIONS.clear()
        app = FastAPI()
        app.include_router(console.router)
        self.client = TestClient(app, base_url='https://test.example')

    def tearDown(self):
        self.page_key.stop()
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
        self.assertEqual(self.client.post(endpoint, json={'page_id': 'test-page', 'x': 12, 'y': 15}).status_code, 403)
        console.SESSIONS['human-test'] = time.time() + 60
        self.client.cookies.set('albatros_provider_test', 'human-test')
        self.assertEqual(self.client.post(endpoint, json={'page_id': 'test-page', 'x': 12, 'y': 15},
            headers={'Origin': 'https://other.example'}).status_code, 403)
        with patch.object(console, 'captcha_regions', AsyncMock(return_value=[
                {'x': 20, 'y': 20, 'width': 100, 'height': 100}])):
            self.assertEqual(self.client.post(endpoint, json={'page_id': 'test-page', 'x': 12, 'y': 15},
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
            response = self.client.post('/api/provider-auth/captcha-click', json={'page_id': 'test-page', 'x': 30, 'y': 30},
                headers={'Origin': 'https://test.example'})
            self.assertEqual(response.status_code, 200)
            mouse.click.assert_awaited_once_with(30, 30)

    def test_stale_provider_form_cannot_transmit_credentials_to_other_page(self):
        console.SESSIONS['human-test'] = time.time() + 60
        self.client.cookies.set('albatros_provider_test', 'human-test')
        with patch.object(console, 'snapshot', AsyncMock()) as capture:
            response = self.client.post('/api/provider-auth/submit', json={
                'page_id': 'old-provider-page', 'form': 0, 'button': 0,
                'fields': {'0': 'fixture-not-transmitted'}}, headers={'Origin': 'https://test.example'})
            self.assertEqual(response.status_code, 409)
            capture.assert_not_awaited()


class ProviderFormBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        from pathlib import Path
        self.driver = await async_playwright().start()
        if not Path(self.driver.chromium.executable_path).exists():
            await self.driver.stop()
            self.skipTest('Playwright Chromium not installed')
        self.browser = await self.driver.chromium.launch(headless=True, args=['--no-sandbox'])
        self.browser_context = await self.browser.new_context()
        self.page = await self.browser_context.new_page()
        self.page_patch = patch.object(console, '_page', self.page)
        self.page_key = patch.object(console, '_page_id', 'browser-test')
        self.url_patch = patch.object(console, 'allowed_url', return_value=True)
        self.page_patch.start()
        self.page_key.start()
        self.url_patch.start()

    async def asyncTearDown(self):
        if hasattr(self, 'browser'):
            self.page_patch.stop()
            self.page_key.stop()
            self.url_patch.stop()
            await self.browser.close()
            await self.driver.stop()

    async def test_email_first_login_with_heading_outside_form(self):
        await self.page.set_content('<h1>Sign In</h1><form><label>Email address<input type="email" '
            'name="email" placeholder="Enter your email"></label><button>Continue</button></form>')
        state = await console.snapshot()
        self.assertEqual(len(state['forms']), 1)
        self.assertEqual(state['forms'][0]['fields'][0]['type'], 'email')
        self.assertEqual(state['forms'][0]['buttons'][0]['text'], 'Continue')
        self.assertNotIn('value', state['forms'][0]['fields'][0])
        self.assertEqual(state['auth_navigation'], [])

    async def test_delayed_screenshot_does_not_block_login_metadata(self):
        from playwright.async_api import TimeoutError
        await self.page.set_content('<h1>Login</h1><form><input type="email"><input type="password"><button>Login</button></form>')
        with patch.object(self.page, 'screenshot', AsyncMock(side_effect=TimeoutError('fixture'))):
            state = await console.snapshot()
        self.assertTrue(state['login_form_present'])
        self.assertFalse(state['screenshot_unavailable'])
        self.assertTrue(state['image'].startswith('data:image/png;base64,'))
        self.assertEqual(len(state['forms']), 1)

    async def test_prepared_fields_do_not_reset_human_verification_on_submit(self):
        import httpx
        await self.page.set_content('''<form><label>Username<input name="username"></label>
          <label>Password<input type="password"></label>
          <textarea name="g-recaptcha-response" hidden></textarea>
          <button type="button" onclick="document.querySelector('#result').textContent=
              document.querySelector('textarea').value?'accepted':'missing'">Login</button></form>
          <p id="result"></p><script>for(const input of document.querySelectorAll('input'))
            input.oninput=()=>document.querySelector('textarea').value='';</script>''')
        app = FastAPI()
        app.include_router(console.router)
        with patch.dict(console.SESSIONS, {'fixture-session': time.time() + 60}), \
             patch.dict(os.environ, {'ALBATROS_PROVIDER_TEST_CONSOLE': 'true'}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url='https://test.example', headers={'Origin': 'https://test.example'},
                    cookies={'albatros_provider_test': 'fixture-session'}) as client:
                body = {'page_id': 'browser-test', 'form': 0, 'button': 0,
                        'fields': {'0': 'fixture-user', '1': 'fixture-password'}, 'prepare_only': True}
                response = await client.post('/api/provider-auth/submit', json=body)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['captcha_pending'])
                self.assertEqual(await self.page.locator('#result').inner_text(), '')
                # Simulate a human-completed response on this local fixture only.
                await self.page.evaluate("document.querySelector('textarea').value='fixture-human-completed'")
                body['prepare_only'] = False
                response = await client.post('/api/provider-auth/submit', json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(await self.page.locator('#result').inner_text(), 'accepted')

    async def test_switching_provider_preserves_authenticated_page(self):
        import httpx
        ctx = self.page.context
        async def respond(route):
            await route.fulfill(status=200, content_type='text/html', body='<h1>Fixture authenticated page</h1>')
        await ctx.route('**/*', respond)
        await self.page.goto('https://apps.dsmpro.ro/')
        app = FastAPI()
        app.include_router(console.router)
        with patch.dict(console.SESSIONS, {'fixture-session': time.time() + 60}), \
             patch.dict(os.environ, {'ALBATROS_PROVIDER_TEST_CONSOLE': 'true'}), \
             patch.object(console, 'context', AsyncMock(return_value=ctx)), \
             patch.object(console, '_provider_pages', {}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url='https://test.example', headers={'Origin': 'https://test.example'},
                    cookies={'albatros_provider_test': 'fixture-session'}) as client:
                response = await client.post('/api/provider-auth/open', json={'provider': 'DSM'})
                self.assertEqual(response.status_code, 200)
                dsm_id = response.json()['page_id']
                response = await client.post('/api/provider-auth/open', json={'provider': 'Inter Cars'})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(self.page.is_closed())
                self.assertNotEqual(response.json()['page_id'], dsm_id)
                response = await client.post('/api/provider-auth/open', json={'provider': 'DSM'})
                self.assertEqual(response.json()['page_id'], dsm_id)

    async def test_device_confirmation_requires_device_context(self):
        content = '<h1>{}</h1><form><input name="denumire"><button>Deconectare</button>' \
                  '<button>SALVARE</button></form>'
        await self.page.set_content(content.format('DISPOZITIV NOU'))
        state = await console.snapshot()
        self.assertIn('SALVARE', [b['text'] for b in state['forms'][0]['buttons']])
        await self.page.set_content(content.format('Catalog produse'))
        state = await console.snapshot()
        self.assertNotIn('SALVARE', [b['text'] for f in state['forms'] for b in f['buttons']])


if __name__ == '__main__':
    unittest.main()
