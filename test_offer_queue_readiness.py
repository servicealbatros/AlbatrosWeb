from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
import app
import browser_resources as resources
from catalogue_probe import source_report
import provider_sessions as sessions
from test_provider_sessions import login_page

VIN = 'WVW00000000000009'


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.primary=self.root/'primary.json'; self.auto=self.root/'auto.json'
        self.row={'id':10,'vin':VIN,'vehicle':'Test car','symptom':'Schimb placute frana fata si spate'}
        self.job={'id':'test-run','request_id':10,'vin':VIN,'customer_request':self.row['symptom'],
                  'status':'QUEUED','created_at':datetime.now().isoformat()}
        self.products=[dict(source='AutoTotal',brand='TEXTAR',requested_item=f'plăcuțe frână {side}',
                           description=f'Set placute frana {side}',position=position,manufacturer_ref='T'+position,
                           supplier_code='AT'+position,price_ron=100)
                       for side,position in [('față','FATA'),('spate','SPATE')]]
        self.primary.write_text(json.dumps(dict(request_id=10,vin=VIN,status='queued',products=[])))
        self.payload=dict(request_id=10,vin=VIN,status='ready',products=self.products,missing_product_kinds=[])
        self.save_auto()
        for item in [patch.object(app,'_offer_result_file',return_value=self.primary),
                     patch.object(app,'_autototal_result_file',return_value=self.auto),
                     patch.object(app,'_request_by_id',return_value=self.row),
                     patch.object(app,'_identity_review_for_request',return_value={'status':'confirmed'}),
                     patch.object(app,'_apply_dsm_history_priority',side_effect=lambda ident,rows:rows),
                     patch.object(app,'_auto_select_complete_dsm_products')]:
            item.start(); self.addCleanup(item.stop)

    def save_auto(self):
        self.auto.write_text(json.dumps(self.payload),encoding='utf-8')

    def test_complete_filtered_priced_auto_result_finishes_queued_job_once(self):
        with patch.object(app,'_load_robot_jobs',return_value=[self.job]), patch.object(app,'_save_robot_jobs') as save:
            self.assertEqual(app._recover_stale_robot_jobs(),1)
            self.assertEqual(self.job['status'],'READY_FOR_REVIEW')
            self.assertEqual(app._recover_stale_robot_jobs(),0)
        save.assert_called_once()
        result=app._load_request_offer_result(10)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(len(result['products']),2)
        self.assertEqual(result['missing_requested_parts'],[])
        self.assertNotEqual(self.job['status'],'APPROVED')
        evidence=source_report(10,VIN,{'intercars':self.primary,'autototal':self.auto})
        self.assertEqual(evidence['sources']['intercars']['status'],'not_completed')
        self.assertEqual(evidence['sources']['autototal']['status'],'ready')

    def test_retry_and_running_are_also_released_by_complete_auto_results(self):
        for status in ('RETRY','RUNNING'):
            self.job['status']=status
            with patch.object(app,'_load_robot_jobs',return_value=[self.job]),patch.object(app,'_save_robot_jobs'):
                self.assertEqual(app._recover_stale_robot_jobs(),1)
            self.assertEqual(self.job['status'],'READY_FOR_REVIEW')

    def test_wrong_vin_or_request_is_not_merged_or_promoted(self):
        for changes in ({'vin':'WRONG'},{'request_id':11}):
            with self.subTest(changes=changes):
                self.payload.update(request_id=10,vin=VIN); self.payload.update(changes); self.save_auto()
                self.assertFalse(app._autototal_complete_offer(10))
                self.assertEqual(app._load_request_offer_result(10)['products'],[])
                self.assertTrue(app._load_request_offer_result(10)['autototal_identity_rejected'])

    def test_every_requested_axle_needs_a_positive_finite_price(self):
        for price in (None,0,-1,float('nan'),float('inf')):
            with self.subTest(price=price):
                self.products[1]['price_ron']=price; self.save_auto()
                self.assertFalse(app._autototal_complete_offer(10))

    def test_missing_axle_or_unconfirmed_identity_does_not_promote(self):
        self.payload['products']=self.products[:1]; self.save_auto()
        self.assertFalse(app._autototal_complete_offer(10))
        self.payload['products']=self.products; self.save_auto()
        with patch.object(app,'_identity_review_for_request',return_value={'status':'needs_confirmation'}):
            self.assertFalse(app._autototal_complete_offer(10))

    def test_unclaimed_queue_gets_a_finite_error_without_sending_messages(self):
        self.payload['status']='running'; self.save_auto()
        self.job['updated_at']=(datetime.now()-timedelta(minutes=10)).isoformat()
        with patch.object(app,'_load_robot_jobs',return_value=[self.job]),patch.object(app,'_save_robot_jobs'), \
             patch.object(app,'send_whatsapp_text',AsyncMock()) as send:
            self.assertEqual(app._recover_stale_robot_jobs(),1)
        self.assertEqual(self.job['status'],'ERROR')
        self.assertEqual(self.job['stage'],'intercars_not_claimed')
        self.assertEqual(json.loads(self.primary.read_text())['status'],'error')
        send.assert_not_awaited()

    def test_provider_session_diagnostics_are_admin_only_and_hide_private_fields(self):
        from starlette.requests import Request
        request=Request({'type':'http','method':'GET','path':'/','headers':[]})
        with self.assertRaises(app.HTTPException) as exc:
            app.master_provider_sessions(request)
        self.assertEqual(exc.exception.status_code,403)
        (self.root/'provider_session_health.json').write_text(json.dumps({
            'Inter Cars':{'status':'connected','auto_login_configured':True,'password':'test-private','username':'test-private'}}))
        with patch.object(app,'_master_access'),patch.object(app,'DATA_DIR',self.root):
            result=app.master_provider_sessions(request)
        self.assertEqual(result['providers']['Inter Cars']['status'],'connected')
        self.assertNotIn('test-private',json.dumps(result))


class QueueLoginTests(unittest.IsolatedAsyncioTestCase):
    def email_page(self, *, foreign=False, human_email=False):
        state={'password':False,'connected':False}
        email=SimpleNamespace(count=AsyncMock(return_value=1),input_value=AsyncMock(return_value='human' if human_email else ''),fill=AsyncMock(),evaluate=AsyncMock(return_value=True))
        hidden=SimpleNamespace(count=AsyncMock(return_value=0))
        login=SimpleNamespace(count=AsyncMock(return_value=1),click=AsyncMock(side_effect=lambda **kw:state.update(connected=True)))
        def button(role, **kwargs):
            self.assertTrue(kwargs['name'].search('Continue'))
            return login
        form=SimpleNamespace(count=AsyncMock(return_value=1),locator=lambda _:hidden,get_by_role=button)
        password=SimpleNamespace(count=AsyncMock(side_effect=lambda:int(state['password'])),input_value=AsyncMock(return_value=''),fill=AsyncMock(),locator=lambda _:form)
        page=SimpleNamespace(url='https://account.intercars.eu/authenticationendpoint/login.do',
                             locator=lambda query:password if 'password' in query else email)
        def continued(**kwargs):
            state['password']=True
            if foreign: page.url='https://untrusted.invalid/login'
        next_button=SimpleNamespace(count=AsyncMock(return_value=1),click=AsyncMock(side_effect=continued))
        page.get_by_role=lambda *a,**kw:next_button
        return page,email,password,next_button,login,state

    async def test_saved_login_handles_email_continue_then_password_only(self):
        page,email,password,next_button,login,state=self.email_page()
        credentials={'username':'test-user','password':'test-secret','login_url':page.url}
        with patch.object(sessions,'active_verification',AsyncMock(return_value=False)), \
             patch.object(sessions,'authenticated',AsyncMock(side_effect=lambda *a:state['connected'])):
            result=await sessions.reconnect(page,credentials,'Inter Cars')
        self.assertEqual(result,'connected')
        email.fill.assert_awaited_once_with('test-user',timeout=10000)
        next_button.click.assert_awaited_once()
        password.fill.assert_awaited_once_with('test-secret',timeout=10000)
        login.click.assert_awaited_once()

    async def test_second_step_rejects_foreign_redirect_and_preserves_human_email(self):
        for foreign,human in ((True,False),(False,True)):
            page,email,password,next_button,login,state=self.email_page(foreign=foreign,human_email=human)
            with patch.object(sessions,'active_verification',AsyncMock(return_value=False)), \
                 patch.object(sessions,'authenticated',AsyncMock(return_value=False)):
                result=await sessions.reconnect(page,{'username':'test-user','password':'test-secret','login_url':page.url},'Inter Cars')
            self.assertEqual(result,'login_location_changed' if foreign else 'human_login_in_progress')
            password.fill.assert_not_awaited(); login.click.assert_not_awaited()
            if human: email.fill.assert_not_awaited(); next_button.click.assert_not_awaited()

    async def test_second_step_stops_at_captcha(self):
        page,email,password,next_button,login,state=self.email_page()
        with patch.object(sessions,'active_verification',AsyncMock(side_effect=lambda *a:state['password'])), \
             patch.object(sessions,'authenticated',AsyncMock(return_value=False)):
            result=await sessions.reconnect(page,{'username':'test-user','password':'test-secret','login_url':page.url},'Inter Cars')
        self.assertEqual(result,'human_verification_required')
        password.fill.assert_not_awaited(); login.click.assert_not_awaited()

    async def test_code_field_is_not_mistaken_for_an_email_step(self):
        page,email,password,next_button,login,state=self.email_page()
        email.evaluate.return_value=False
        with patch.object(sessions,'active_verification',AsyncMock(return_value=False)):
            result=await sessions.reconnect(page,{'username':'test-user','password':'test-secret','login_url':page.url},'Inter Cars')
        self.assertEqual(result,'unsupported_login_form')
        email.fill.assert_not_awaited(); next_button.click.assert_not_awaited()

    async def test_queued_intercars_can_reconnect_but_running_cannot(self):
        for state,allowed in [('QUEUED',True),('RETRY',True),('RUNNING',False)]:
            with self.subTest(state=state), TemporaryDirectory() as folder:
                root=Path(folder)
                (root/'robot_offer_jobs.json').write_text(json.dumps([{'status':state}]))
                keeper=sessions.ProviderSessionKeeper(root/'profile',Fernet.generate_key().decode())
                url='https://account.intercars.eu/authenticationendpoint/login.do'
                keeper.vaults['Inter Cars'].save('user','test-secret',url)
                page,*_=login_page(url)
                with patch.dict(os.environ,{'ALBATROS_DATA_DIR':folder}), \
                     patch.object(sessions,'authenticated',AsyncMock(return_value=False)), \
                     patch.object(sessions,'upstream_error',AsyncMock(return_value=False)), \
                     patch.object(sessions,'reconnect',AsyncMock(return_value='connected')) as reconnect:
                    result=await keeper.check(SimpleNamespace(pages=[page]))
                if allowed:
                    reconnect.assert_awaited_once(); self.assertEqual(result['Inter Cars']['status'],'connected')
                else:
                    reconnect.assert_not_awaited()
                self.assertEqual(resources.busy_reason(root),'offering')  # Housekeeping protection stays strict.

    def test_manual_console_and_corrupt_state_still_block_reconnection(self):
        with TemporaryDirectory() as folder,patch.dict(os.environ,{'ALBATROS_DATA_DIR':folder}):
            root=Path(folder); keeper=sessions.ProviderSessionKeeper(root/'profile',Fernet.generate_key().decode())
            marker=root/'.browser-console-activity'; marker.touch()
            self.assertFalse(keeper.idle(allow_queued=True))
            marker.unlink()
            (root/'robot_offer_jobs.json').write_text('broken')
            self.assertFalse(keeper.idle(allow_queued=True))


if __name__=='__main__':
    unittest.main()
