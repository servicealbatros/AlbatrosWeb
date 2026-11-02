import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from cryptography.fernet import Fernet, InvalidToken
import provider_sessions as sessions


class ProviderVaultTests(unittest.TestCase):
    def test_credentials_are_encrypted_persistent_and_bound_to_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            key=Fernet.generate_key().decode(); profile=Path(folder)/'profile'
            vault=sessions.ProviderLoginVault(profile,key,'AutoTotal')
            vault.save('test-user','test-secret','https://www3.eoriginal.ro/login?temporary=discarded')
            self.assertNotIn(b'test-secret',vault.path.read_bytes())
            self.assertNotIn(b'test-user',vault.path.read_bytes())
            restored=sessions.ProviderLoginVault(profile,key,'AutoTotal').load()
            self.assertEqual(restored['password'],'test-secret')
            self.assertEqual(restored['login_url'],'https://www3.eoriginal.ro/login')
            self.assertIsNone(sessions.ProviderLoginVault(profile,key,'Inter Cars').load())
            with self.assertRaises(InvalidToken):
                sessions.ProviderLoginVault(profile,Fernet.generate_key().decode(),'AutoTotal').load()

    def test_untrusted_domains_and_unknown_provider_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            key=Fernet.generate_key().decode();vault=sessions.ProviderLoginVault(Path(folder)/'profile',key,'AutoTotal')
            for url in ['http://www3.eoriginal.ro/login','https://www3.eoriginal.ro.evil.test/login','https://user@www3.eoriginal.ro/login','https://ro.e-cat.intercars.eu/login']:
                with self.assertRaises(ValueError):vault.save('user','secret',url)
            self.assertFalse(vault.path.exists())
            with self.assertRaises(ValueError):sessions.ProviderLoginVault(Path(folder)/'profile',key,'Unknown')


def login_page(url='https://www3.eoriginal.ro/login'):
    password=SimpleNamespace(count=AsyncMock(return_value=1),input_value=AsyncMock(return_value=''),fill=AsyncMock(),evaluate=AsyncMock(return_value=False))
    username=SimpleNamespace(count=AsyncMock(return_value=1),input_value=AsyncMock(return_value=''),fill=AsyncMock())
    button=SimpleNamespace(count=AsyncMock(return_value=1),click=AsyncMock())
    form=SimpleNamespace(count=AsyncMock(return_value=1),locator=lambda _:username,get_by_role=lambda *a,**kw:button)
    password.locator=lambda _:form
    page=SimpleNamespace(url=url,locator=lambda _:password,is_closed=lambda:False)
    return page,password,username,button


class ProviderLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_autototal_icon_menu_is_positive_evidence_but_login_is_not(self):
        from playwright.async_api import async_playwright, Error
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Error as exc:
                if "Executable doesn't exist" in str(exc):
                    self.skipTest('Playwright Chromium is not installed')
                raise
            try:
                page = await browser.new_page()
                markup = '<a href="" class="dropdown-item"><i>exit_to_app</i><span>Deconectare</span></a>'
                await page.route('**/*', lambda route: route.fulfill(body=markup, content_type='text/html'))
                await page.goto('https://www3.eoriginal.ro/')
                self.assertTrue(await sessions.authenticated(page, 'AutoTotal'))
                await page.set_content(markup + '<input type="password">')
                self.assertFalse(await sessions.authenticated(page, 'AutoTotal'))
                await page.set_content('<div>Catalog auto</div>')
                self.assertFalse(await sessions.authenticated(page, 'AutoTotal'))
                await page.goto('https://untrusted.example/')
                self.assertFalse(await sessions.authenticated(page, 'AutoTotal'))
            finally:
                await browser.close()

    async def test_completed_captcha_does_not_get_reset_by_identical_credentials(self):
        page,password,username,button=login_page()
        password.input_value.return_value='test-secret'
        username.input_value.return_value='test-user'
        credentials={'username':'test-user','password':'test-secret','login_url':page.url}
        with patch.object(sessions,'active_verification',AsyncMock(return_value=False)),patch.object(sessions,'authenticated',AsyncMock(return_value=True)):
            self.assertEqual(await sessions.reconnect(page,credentials,'AutoTotal'),'connected')
        username.fill.assert_not_awaited();password.fill.assert_not_awaited()
        button.click.assert_awaited_once()

    async def test_expired_form_is_refreshed_with_get_not_post_resubmission(self):
        with tempfile.TemporaryDirectory() as folder:
            keeper=sessions.ProviderSessionKeeper(Path(folder)/'profile',Fernet.generate_key().decode())
            p,_,_,button=login_page();p.goto=AsyncMock();p.reload=AsyncMock()
            with patch.object(sessions,'authenticated',AsyncMock(return_value=False)), \
                    patch.object(sessions,'expired_autototal_form',AsyncMock(return_value=True)), \
                    patch.object(keeper,'idle',return_value=True):
                first=await keeper.check(SimpleNamespace(pages=[p]));second=await keeper.check(SimpleNamespace(pages=[p]))
            self.assertEqual(first['AutoTotal']['status'],'login_form_refreshed')
            self.assertEqual(second['AutoTotal']['status'],'login_form_expired')
            p.goto.assert_awaited_once_with(sessions.AUTOTOTAL_LOGIN,wait_until='domcontentloaded',timeout=15000)
            p.reload.assert_not_awaited();button.click.assert_not_awaited()

    async def test_connected_autototal_session_is_kept_alive_without_navigation(self):
        with tempfile.TemporaryDirectory() as folder:
            keeper=sessions.ProviderSessionKeeper(Path(folder)/'profile',Fernet.generate_key().decode())
            p,_,_,_=login_page(sessions.AUTOTOTAL_HOME);p.goto=AsyncMock();p.reload=AsyncMock()
            response=SimpleNamespace(url=sessions.AUTOTOTAL_HOME,status=200,ok=True,dispose=AsyncMock())
            p.request=SimpleNamespace(get=AsyncMock(return_value=response))
            with patch.object(sessions,'authenticated',AsyncMock(return_value=True)),patch.object(keeper,'idle',return_value=True):
                first=await keeper.check(SimpleNamespace(pages=[p]));await keeper.check(SimpleNamespace(pages=[p]))
            self.assertEqual(first['AutoTotal']['status'],'connected')
            p.request.get.assert_awaited_once_with(sessions.AUTOTOTAL_HOME,timeout=10000)
            response.dispose.assert_awaited_once()
            p.goto.assert_not_awaited();p.reload.assert_not_awaited()

    async def test_missing_autototal_tab_is_restored_once_in_same_context(self):
        with tempfile.TemporaryDirectory() as folder:
            keeper=sessions.ProviderSessionKeeper(Path(folder)/'profile',Fernet.generate_key().decode())
            keeper.vaults['AutoTotal'].save('user','secret',sessions.AUTOTOTAL_HOME,strict_location=False)
            p,_,_,_=login_page();p.goto=AsyncMock()
            context=SimpleNamespace(pages=[],new_page=AsyncMock(return_value=p))
            with patch.object(sessions,'authenticated',AsyncMock(return_value=False)), \
                    patch.object(sessions,'upstream_error',AsyncMock(return_value=False)), \
                    patch.object(sessions,'expired_autototal_form',AsyncMock(return_value=False)), \
                    patch.object(sessions,'reconnect',AsyncMock(return_value='human_verification_required')), \
                    patch.object(keeper,'idle',return_value=True):
                first=await keeper.check(context);await keeper.check(context)
            self.assertTrue(first['AutoTotal']['tab_restored'])
            context.new_page.assert_awaited_once()

    async def test_busy_offer_prevents_expired_form_navigation(self):
        with tempfile.TemporaryDirectory() as folder:
            keeper=sessions.ProviderSessionKeeper(Path(folder)/'profile',Fernet.generate_key().decode())
            p,_,_,_=login_page();p.goto=AsyncMock()
            with patch.object(sessions,'authenticated',AsyncMock(return_value=False)), \
                    patch.object(sessions,'expired_autototal_form',AsyncMock(return_value=True)),patch.object(keeper,'idle',return_value=False):
                await keeper.check(SimpleNamespace(pages=[p]))
            p.goto.assert_not_awaited()

    async def test_saved_password_only_submitted_to_pinned_trusted_form(self):
        page,password,username,button=login_page()
        creds={'username':'test-user','password':'test-secret','login_url':page.url}
        with patch.object(sessions,'active_verification',AsyncMock(return_value=False)),patch.object(sessions,'authenticated',AsyncMock(return_value=True)):
            self.assertEqual(await sessions.reconnect(page,creds,'AutoTotal'),'connected')
        username.fill.assert_awaited_once_with('test-user',timeout=10000)
        password.fill.assert_awaited_once_with('test-secret',timeout=10000)
        button.click.assert_awaited_once()

    async def test_password_not_filled_on_changed_location_or_other_host(self):
        for url in ['https://www3.eoriginal.ro/new-login','https://evil.test/login']:
            page,password,_,button=login_page(url)
            await sessions.reconnect(page,{'username':'user','password':'secret','login_url':'https://www3.eoriginal.ro/login'},'AutoTotal')
            password.fill.assert_not_awaited();button.click.assert_not_awaited()

    async def test_captcha_or_human_typing_is_left_to_person(self):
        page,password,_,button=login_page();creds={'username':'user','password':'secret','login_url':page.url}
        with patch.object(sessions,'active_verification',AsyncMock(return_value=True)):
            self.assertEqual(await sessions.reconnect(page,creds,'AutoTotal'),'human_verification_required')
        password.fill.assert_not_awaited();button.click.assert_not_awaited()
        password.input_value.return_value='human-entered'
        with patch.object(sessions,'active_verification',AsyncMock(return_value=False)):
            self.assertEqual(await sessions.reconnect(page,creds,'AutoTotal'),'human_login_in_progress')
        password.fill.assert_not_awaited()

    async def test_keeper_uses_backoff_instead_of_repeating_failed_password(self):
        with tempfile.TemporaryDirectory() as folder:
            keeper=sessions.ProviderSessionKeeper(Path(folder)/'profile',Fernet.generate_key().decode())
            vault=keeper.vaults['AutoTotal'];vault.save('user','secret','https://www3.eoriginal.ro/login')
            page,_,_,_=login_page();context=SimpleNamespace(pages=[page])
            attempt=AsyncMock(return_value='login_failed')
            with patch.object(sessions,'authenticated',AsyncMock(return_value=False)),patch.object(sessions,'upstream_error',AsyncMock(return_value=False)),patch.object(sessions,'reconnect',attempt),patch.object(sessions.time,'monotonic',return_value=10000):
                first=await keeper.check(context);second=await keeper.check(context)
            self.assertEqual(first['AutoTotal']['status'],'login_failed')
            self.assertEqual(second['AutoTotal']['status'],'retry_wait')
            attempt.assert_awaited_once()

    async def test_gateway_error_reload_does_not_submit_password(self):
        with tempfile.TemporaryDirectory() as folder:
            keeper=sessions.ProviderSessionKeeper(Path(folder)/'profile',Fernet.generate_key().decode());page,_,_,button=login_page();page.reload=AsyncMock()
            with patch.object(sessions,'authenticated',AsyncMock(return_value=False)),patch.object(sessions,'upstream_error',AsyncMock(return_value=True)):
                result=await keeper.check(SimpleNamespace(pages=[page]))
            self.assertEqual(result['AutoTotal']['status'],'upstream_unavailable')
            page.reload.assert_awaited_once();button.click.assert_not_awaited()

    async def test_one_supplier_error_does_not_stop_others(self):
        with tempfile.TemporaryDirectory() as folder:
            keeper=sessions.ProviderSessionKeeper(Path(folder)/'profile',Fernet.generate_key().decode());page,_,_,_=login_page()
            with patch.object(sessions,'authenticated',AsyncMock(side_effect=RuntimeError('unavailable'))):
                result=await keeper.check(SimpleNamespace(pages=[page]))
            self.assertEqual(result['AutoTotal']['status'],'browser_unavailable')
            self.assertEqual(result['Haynes']['status'],'standby')


class ProviderConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_supplier_configuration_is_encrypted_and_readback_has_no_credentials(self):
        import os
        import provider_test_console as console
        from starlette.requests import Request
        with tempfile.TemporaryDirectory() as folder:
            key=Fernet.generate_key().decode();profile=Path(folder)/'profile'
            request=Request({'type':'http','headers':[(b'host',b'test.local'),(b'origin',b'https://test.local')]})
            env={'ALBATROS_PROVIDER_STATE_KEY':key,'ALBATROS_BROWSER_PROFILE_DIR':str(profile),'ALBATROS_DATA_DIR':folder}
            with patch.dict(os.environ,env),patch.object(console,'session'):
                result=await console.configure_supplier_login(console.SupplierCredentials(provider='AutoTotal',username='fixture-user',password='fixture-password'),request)
                state=await console.reconnection_status(request)
            self.assertTrue(result['ok']);self.assertTrue(state['providers']['AutoTotal']['configured'])
            import json
            self.assertNotIn('fixture-',json.dumps(state))
            saved=sessions.ProviderLoginVault(profile,key,'AutoTotal').load()
            self.assertFalse(saved['strict_location'])
            self.assertEqual(saved['password'],'fixture-password')

    async def test_unauthorized_and_cross_origin_configuration_are_rejected(self):
        import provider_test_console as console
        from fastapi import HTTPException
        from starlette.requests import Request
        request=Request({'type':'http','headers':[(b'host',b'test.local'),(b'origin',b'https://other.local')]})
        body=console.SupplierCredentials(provider='AutoTotal',username='fixture',password='fixture-password')
        with patch.object(console,'session',side_effect=HTTPException(403,'Expired')):
            with self.assertRaises(HTTPException):await console.configure_supplier_login(body,request)
        with patch.object(console,'session'):
            with self.assertRaises(HTTPException) as error:await console.configure_supplier_login(body,request)
            self.assertEqual(error.exception.status_code,403)


if __name__=='__main__':unittest.main()
