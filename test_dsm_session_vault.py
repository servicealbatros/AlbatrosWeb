import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken
from dsm_session_vault import DsmSessionVault

COOKIE = {'name':'fixture-session','value':'private-fixture-token','domain':'appm.dsmpro.ro','path':'/','expires':-1,'httpOnly':True,'secure':True,'sameSite':'Lax'}
LANDING = 'https://appm.dsmpro.ro/index.php/special/home/index'

class VaultTests(unittest.TestCase):
    def test_encrypted_session_cookies_roundtrip_and_domain_scope(self):
        with tempfile.TemporaryDirectory() as root:
            key = Fernet.generate_key().decode()
            vault = DsmSessionVault(Path(root)/'profile',key)
            vault.save([COOKIE, {**COOKIE,'domain':'other.example'}, {**COOKIE,'name':'expired','expires':time.time()-1}],LANDING+'?private=query')
            self.assertNotIn(b'private-fixture-token',vault.path.read_bytes())
            self.assertEqual(vault.load()['cookies'],[COOKIE])
            self.assertEqual(vault.load()['landing'],LANDING)
            with self.assertRaises(InvalidToken):
                DsmSessionVault(Path(root)/'profile',Fernet.generate_key().decode()).load()

    def test_login_page_cannot_replace_saved_authenticated_session(self):
        with tempfile.TemporaryDirectory() as root:
            vault = DsmSessionVault(Path(root)/'profile',Fernet.generate_key().decode())
            vault.save([COOKIE],LANDING)
            before = vault.path.read_bytes()
            with self.assertRaises(ValueError):
                vault.save([COOKIE],'https://appm.dsmpro.ro/index.php/auth/login')
            self.assertEqual(vault.path.read_bytes(),before)

class BrowserRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_backend_cdp_connection_needs_no_browser_origin(self):
        import json
        from urllib.request import urlopen
        import websockets
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                self.skipTest('Chromium not installed')
            with tempfile.TemporaryDirectory() as root:
                context = await playwright.chromium.launch_persistent_context(root, headless=True,
                    args=['--no-sandbox','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0'])
                port = (Path(root)/'DevToolsActivePort').read_text().splitlines()[0]
                targets = json.load(urlopen('http://127.0.0.1:'+port+'/json/list',timeout=3))
                target = next(t for t in targets if t['type']=='page')
                async with websockets.connect(target['webSocketDebuggerUrl'],open_timeout=3) as socket:
                    await socket.send(json.dumps({'id':1,'method':'Runtime.evaluate','params':{'expression':'1+1','returnByValue':True}}))
                    while True:
                        message = json.loads(await asyncio.wait_for(socket.recv(),timeout=3))
                        if message.get('id') == 1:
                            self.assertEqual(message['result']['result']['value'],2)
                            break
                await context.close()

    async def test_session_cookie_is_restored_after_real_browser_restart(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                self.skipTest('Chromium not installed')
            with tempfile.TemporaryDirectory() as root:
                profile = Path(root)/'profile'
                vault = DsmSessionVault(profile,Fernet.generate_key().decode())
                first = await playwright.chromium.launch_persistent_context(str(profile),headless=True,args=['--no-sandbox'])
                await first.add_cookies([COOKIE])
                vault.save(await first.cookies(),LANDING)
                await first.close()
                second = await playwright.chromium.launch_persistent_context(str(profile),headless=True,args=['--no-sandbox'])
                before = await second.cookies(LANDING)
                self.assertFalse(any(c['name']=='fixture-session' for c in before))
                await second.add_cookies(vault.load()['cookies'])
                restored = await second.cookies(LANDING)
                self.assertTrue(any(c['name']=='fixture-session' and c['value']=='private-fixture-token' for c in restored))
                await second.close()
