"""Run in a separate process: uses a new empty database and dummy secrets."""
import gc
import hashlib
import hmac
import os
from pathlib import Path
import tempfile
import unittest

_test_data = tempfile.TemporaryDirectory()
os.environ.update({
    "ALBATROS_DATA_DIR": _test_data.name,
    "ALBATROS_OFFERING_DIR": str(Path(_test_data.name) / "offering"),
    "OPENAI_API_KEY": "test-placeholder",
    "ADMIN_API_TOKEN": "test-administrator-secret",
    "RECEPTION_DASHBOARD_USER": "test-user",
    "RECEPTION_DASHBOARD_PASSWORD": "test-password",
    "DASHBOARD_SESSION_SECRET": "test-signed-session-secret",
    "ROBOT_ENABLED": "false",
    "BETA_MODE": "true",
    "WHATSAPP_APP_SECRET": "test-meta-secret",
    "WHATSAPP_VERIFY_TOKEN": "test-verification",
    "WHATSAPP_ACCESS_TOKEN": "",
    "ALLOWED_RECEPTION_IPS": "",
})

from starlette.testclient import TestClient
from cloud_app import app
from security import POLICY


class CloudAccessTests(unittest.TestCase):
    def test_morning_tick_sends_once_on_weekday_and_never_on_weekend(self):
        import asyncio
        import app as core
        from datetime import datetime
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, PropertyMock, patch
        async def run():
            with TemporaryDirectory() as root, patch.object(core, 'DB_PATH', Path(root) / 'scheduler.db'), patch.object(
                    type(POLICY), 'robot_enabled', new_callable=PropertyMock, return_value=True), patch.dict(os.environ, {
                    'MECHANIC_SCHEDULE_SEND_TIME':'08:00', 'MECHANIC_SCHEDULE_AUTOMATIC_ENABLED':'true'}), patch.object(
                    core, '_send_all_technician_schedules_for_date', AsyncMock(return_value={'ok':True})) as send:
                await core._mechanic_schedule_tick(datetime(2026,10,17,8))
                send.assert_not_awaited()
                await core._mechanic_schedule_tick(datetime(2026,10,15,7,59))
                send.assert_not_awaited()
                await core._mechanic_schedule_tick(datetime(2026,10,15,8))
                await core._mechanic_schedule_tick(datetime(2026,10,15,8,1))
                self.assertEqual(send.await_count, 1)
                self.assertTrue(send.call_args.kwargs['automatic'])
        asyncio.run(run())

    def test_automatic_batch_skips_already_accepted_employee_after_retry(self):
        import asyncio
        import app as core
        from datetime import datetime
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, patch
        from dsm_schedule_sync import ensure_automation_tables
        async def run():
            with TemporaryDirectory() as root:
                db = Path(root) / 'scheduler.db'
                ensure_automation_tables(db)
                employee = {'id':1,'name':'Fixture employee','phone':'0700100003'}
                jobs = [{'id':1,'technician_id':1,'start_at':'2026-10-15T08:30:00','end_at':'2026-10-15T10:30:00','operations':'Revizie','registration':'TEST','vehicle':'Fixture'}]
                with patch.object(core,'DB_PATH',db), patch.object(core,'scheduler_list_technicians',return_value=[employee]), patch.object(
                        core,'scheduler_list_appointments',return_value=jobs), patch.object(core,'send_whatsapp_notification',AsyncMock(return_value={'messages':[{'id':'fixture-message'}]})) as send, patch.object(
                        core.asyncio,'sleep',AsyncMock()), patch.dict(core.WHATSAPP_DELIVERY_STATUS,{},clear=True):
                    first = await core._send_all_technician_schedules_for_date(datetime(2026,10,15),automatic=True)
                    second = await core._send_all_technician_schedules_for_date(datetime(2026,10,15),automatic=True)
                    self.assertEqual(send.await_count,1)
                    self.assertEqual(len(first['sent']),1)
                    self.assertEqual(second['skipped'][0]['reason'],'deja trimis astăzi')
        asyncio.run(run())

    def test_password_login_without_device_approval_and_separate_master_access(self):
        from unittest.mock import patch
        import app as core
        core.init_scheduler_db(core.DB_PATH)
        core.ensure_automation_tables(core.DB_PATH)
        with patch.dict(os.environ, {'DASHBOARD_DEVICE_APPROVAL': 'false', 'MASTER_DASHBOARD_USER': 'master',
                'MASTER_DASHBOARD_PASSWORD': 'distinct-master-password', 'RECEPTION_BRIDGE_URL': '', 'ALBATROS_PROVIDER_TEST_CONSOLE':'true'}):
            client = TestClient(app, base_url='https://app.servicealbatros.ro')
            self.assertEqual(client.post('/receptie/login', data={'username': 'test-user', 'password': 'wrong'}).status_code, 401)
            result = client.post('/receptie/login', data={'username': 'test-user', 'password': 'test-password'}, follow_redirects=False)
            self.assertEqual(result.status_code, 303)
            self.assertNotIn('albatros_device', client.cookies)
            self.assertEqual(client.get('/api/dashboard/master/status').status_code, 403)
            self.assertEqual(client.post('/api/dashboard/master/provider-login',json={},headers={'Origin':'https://app.servicealbatros.ro'}).status_code,403)
            result = client.post('/receptie/login', data={'username': 'master', 'password': 'distinct-master-password'}, follow_redirects=False)
            self.assertEqual(result.headers['location'], '/master')
            self.assertEqual(client.get('/api/dashboard/master/status').status_code, 200)
            grant = client.post('/api/dashboard/master/provider-login',json={},headers={'Origin':'https://app.servicealbatros.ro'})
            self.assertEqual(grant.status_code,200)
            self.assertIn('/provider-auth#',grant.json()['url'])
            self.assertEqual(client.post('/api/dashboard/master/robot', json={'enabled': False}, headers={'Origin':'https://attacker.example'}).status_code, 403)
            token = client.cookies.get('albatros_reception')
            self.assertTrue(POLICY.is_master_session(token))
            with patch.dict(os.environ, {'MASTER_DASHBOARD_PASSWORD': 'changed-password'}):
                self.assertIsNone(POLICY.verify_dashboard_session(token))

    def test_password_change_is_hashed_persistent_and_revokes_previous_session(self):
        from unittest.mock import patch
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as root, patch.object(POLICY, 'data_dir', Path(root)), patch.dict(os.environ, {'DASHBOARD_DEVICE_APPROVAL':'false'}):
            token = POLICY.create_dashboard_session('test-user', 'test-password')
            self.assertTrue(POLICY.verify_dashboard_session(token))
            POLICY.change_dashboard_password(False, 'a-new-test-password')
            saved = (Path(root) / 'dashboard_passwords.json').read_text()
            self.assertNotIn('a-new-test-password', saved)
            self.assertIn('pbkdf2$', saved)
            self.assertIsNone(POLICY.verify_dashboard_session(token))
            self.assertFalse(POLICY.dashboard_credentials_valid('test-user', 'test-password'))
            self.assertTrue(POLICY.dashboard_credentials_valid('test-user', 'a-new-test-password'))

    def test_remote_gateway_signature_can_reach_internal_worker_only_when_valid(self):
        import time
        from unittest.mock import AsyncMock, patch
        from starlette.responses import JSONResponse
        from reception_bridge import signature
        stamp, nonce, actor = str(int(time.time())), 'cloud-worker-valid-fixture', 'consilier'
        path = '/api/requests'
        secret = 'test-bridge-secret-' * 3
        headers = {'x-albatros-time': stamp, 'x-albatros-nonce': nonce,
                   'x-albatros-actor': actor,
                   'x-albatros-signature': signature(secret, stamp, nonce, actor, 'GET', path, b'')}
        with patch.dict(os.environ, {'ALBATROS_INTERNAL_BRIDGE': 'true', 'RECEPTION_BRIDGE_SECRET': secret}), patch(
                'cloud_app.forward', new_callable=AsyncMock, return_value=JSONResponse({'worker': True})) as proxy:
            client = TestClient(app, base_url='https://worker.example.com')
            result = client.get(path, headers=headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(proxy.call_count, 1)
            self.assertEqual(client.get(path, headers=headers).status_code, 403)  # replay
            self.assertEqual(client.get(path).status_code, 401)
            self.assertEqual(proxy.call_count, 1)

    def test_private_worker_listener_never_opens_public_gateway(self):
        from unittest.mock import patch
        from cloud_app import backend
        with patch.dict(os.environ, {'ALBATROS_INTERNAL_WORKER_API': 'true'}):
            private = TestClient(backend, base_url='http://127.0.0.1:8010')
            public = TestClient(backend, base_url='http://127.0.0.1:10000')
            gateway = TestClient(app, base_url='https://app.servicealbatros.ro:8010')
            self.assertEqual(private.get('/api/robot/health').status_code, 200)
            self.assertEqual(public.get('/api/robot/health').status_code, 403)
            self.assertEqual(private.get('/api/robot/health', headers={'X-Forwarded-For': '203.0.113.9'}).status_code, 403)
            self.assertEqual(gateway.get('/api/robot/health').status_code, 401)

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app, base_url="https://app.servicealbatros.ro")

    def setUp(self):
        self.client.cookies.clear()

    def test_anonymous_cannot_read_application_even_from_local_address(self):
        for path in ("/cereri", "/programator", "/oferta/27", "/api/requests/27/offer-products",
                     "/api/scheduler/technicians", "/static/chat.html", "/api/chat",
                     "/albatros-a", "/docs", "/openapi.json", "/api/admin/metrics"):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual(response.status_code, 401)

    def test_html_visitors_are_redirected_and_health_is_minimal(self):
        response = self.client.get("/cereri", headers={"accept":"text/html"}, follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.client.get("/health").json(), {"ok":True})
        login = self.client.get("/receptie/login")
        self.assertEqual(login.status_code,200)
        self.assertNotIn("Robotul", login.text)
        self.assertNotIn("DSM", login.text)

    def test_only_valid_meta_verification_is_public(self):
        self.assertEqual(self.client.get("/webhook/whatsapp").status_code,403)
        response = self.client.get("/webhook/whatsapp", params={"hub.mode":"subscribe",
                   "hub.verify_token":"test-verification","hub.challenge":"12345"})
        self.assertEqual(response.text,"12345")

    def test_meta_messages_require_valid_signature(self):
        previous = POLICY._robot_override
        POLICY._robot_override = True
        try:
            body = b'{"entry": []}'
            self.assertEqual(self.client.post('/webhook/whatsapp',content=body).status_code,401)
            signature = 'sha256=' + hmac.new(b'test-meta-secret',body,hashlib.sha256).hexdigest()
            response = self.client.post('/webhook/whatsapp',content=body,
                       headers={'X-Hub-Signature-256':signature,'Content-Type':'application/json'})
            self.assertEqual(response.status_code,200)
        finally:
            POLICY._robot_override = previous

    def test_wrong_password_and_unapproved_device_do_not_grant_access(self):
        wrong = self.client.post("/receptie/login", data={"username":"test-user","password":"wrong"})
        self.assertEqual(wrong.status_code,401)
        pending = self.client.post("/receptie/login", data={"username":"test-user","password":"test-password"})
        self.assertEqual(pending.status_code,403)
        self.assertEqual(self.client.get("/cereri").status_code,401)

    def test_backups_require_admin_and_reject_corrupt_archives(self):
        import hashlib
        body = b'not-an-archive'
        self.assertEqual(self.client.post('/api/admin/worker-backup', content=body).status_code,403)
        response = self.client.post('/api/admin/worker-backup', content=body, headers={
            'Authorization': 'Bearer test-administrator-secret',
            'X-Backup-SHA256': hashlib.sha256(body).hexdigest()})
        self.assertEqual(response.status_code,400)

    def test_approved_login_works_and_revocation_blocks_existing_session(self):
        device,_ = POLICY.devices.register(None,"Test authorized device","testclient")
        POLICY.devices.change(device,True)
        self.client.cookies.set("albatros_device",device)
        response = self.client.post("/receptie/login", data={"username":"test-user","password":"test-password"},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.client.get("/api/dashboard/access-check").status_code,200)
        self.assertEqual(self.client.post("/api/requests/27/offer-start").status_code,503)
        POLICY.devices.change(device,False)
        self.assertEqual(self.client.get("/api/dashboard/access-check").status_code,401)


if __name__ == "__main__":
    try:
        unittest.main(exit=False)
    finally:
        gc.collect()
        _test_data.cleanup()
