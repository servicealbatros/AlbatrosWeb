import asyncio
import ast
from contextlib import closing
import gc
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from unittest.mock import Mock
from types import SimpleNamespace
from fastapi import HTTPException
import database
import dsm_accepted_offers as sync


def payload(state='signed', status='working', children=None):
 return {'transaction':{'transaction_head__type':'1','transaction_head__status':status},'sms_or_qrcode_sended':{'service_offer':state},'transaction_head_childs':children or []}


def item(ident='1',code='OFERTA-1'):
 return {'id':ident,'reason':'accepted','signature':'signed','code':code,'detail':{'transaction_id':ident,'document_code':code,'registration':'B100TST','vin':'WBA00000000000002','vehicle':'BMW','customer_name':'Fixture client','customer_phone':'0700100003','description':'Schimb plăcuțe față și spate','parts':[{'name':'Plăcuțe','code':'P1','quantity':2,'historical_price_ron':100}],'labors':[{'name':'Înlocuire plăcuțe','hours':1,'quantity':1,'historical_price_ron':200}]}}


class SignedOfferTests(unittest.TestCase):
 def status_endpoint(self):
  # Exercise the actual endpoint and persistence functions without starting the robot.
  tree=ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8-sig'))
  nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'set_request_status','_persist_request_status'}]
  for node in nodes:node.decorator_list=[]
  future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
  def lookup(ident):
   with sync.connection(self.db) as conn:
    conn.row_factory=sqlite3.Row
    row=conn.execute('SELECT * FROM service_requests WHERE id=?',(ident,)).fetchone()
    return dict(row) if row else None
  env={'Header':lambda *a,**k:None,'Action':SimpleNamespace(MODIFY_BOOKING='booking'),
       '_confirm_local_dashboard_click':Mock(),'_request_by_id':lookup,
       '_request_has_approved_offer':Mock(return_value=False),'HTTPException':HTTPException,
       'VALID_REQUEST_STATUSES':{'nou','acceptat','programat','rezolvat'},
       'DB_PATH':self.db,'REQUEST_STATUS':{},'save_request_status':Mock(),'sqlite3':sqlite3,'closing':closing}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[future,*nodes],type_ignores=[])),'app.py','exec'),env)
  return env
 def test_signed_dsm_offer_can_be_marked_programmed_without_local_draft(self):
  ident=sync.apply_snapshot(self.db,[item()])['request_ids'][0]
  env=self.status_endpoint()
  result=env['set_request_status'](ident,SimpleNamespace(status='programat'),object(),'update-request-status')
  self.assertEqual(result,{'request_id':ident,'status':'programat'})
  self.assertEqual(env['REQUEST_STATUS'][ident],'programat')
  sync.apply_snapshot(self.db,[item()])
  with sync.connection(self.db) as conn:
   self.assertEqual(conn.execute('SELECT status FROM service_requests WHERE id=?',(ident,)).fetchone()[0],'programat')
   self.assertEqual(conn.execute('SELECT COUNT(*) FROM scheduler_appointments').fetchone()[0],0)
 def test_status_gate_still_blocks_unsigned_closed_missing_and_local_unapproved_offers(self):
  ident=sync.apply_snapshot(self.db,[item()])['request_ids'][0]
  env=self.status_endpoint()
  for signature,eligibility in [('update','accepted'),('signed','closed'),('signed','unsigned'),('signed','converted')]:
   with sync.connection(self.db) as conn:
    conn.execute('UPDATE dsm_accepted_offer_links SET signature_state=?,eligibility=?',(signature,eligibility))
   with self.subTest(signature=signature,eligibility=eligibility),self.assertRaises(HTTPException) as caught:
    env['set_request_status'](ident,SimpleNamespace(status='programat'),object(),'update-request-status')
   self.assertEqual(caught.exception.status_code,409)
  with sync.connection(self.db) as conn:
   conn.execute("UPDATE dsm_accepted_offer_links SET signature_state='signed',eligibility='accepted'")
   conn.execute("UPDATE service_requests SET source='whatsapp' WHERE id=?",(ident,))
  with self.assertRaises(HTTPException) as caught:
   env['set_request_status'](ident,SimpleNamespace(status='acceptat'),object(),'update-request-status')
  self.assertEqual(caught.exception.status_code,409)
  with sync.connection(self.db) as conn:
   conn.execute("UPDATE service_requests SET source='dsm_accepted_offer' WHERE id=?",(ident,))
   conn.execute('DELETE FROM dsm_accepted_offer_links')
  with self.assertRaises(HTTPException):
   env['set_request_status'](ident,SimpleNamespace(status='programat'),object(),'update-request-status')
  env['save_request_status'].assert_not_called()
 def test_local_advisor_approved_offer_can_still_be_programmed(self):
  ident=sync.apply_snapshot(self.db,[item()])['request_ids'][0]
  with sync.connection(self.db) as conn:conn.execute("UPDATE service_requests SET source='whatsapp' WHERE id=?",(ident,))
  env=self.status_endpoint();env['_request_has_approved_offer'].return_value=True
  self.assertEqual(env['set_request_status'](ident,SimpleNamespace(status='programat'),object())['status'],'programat')
 def test_acceptance_date_never_uses_creation_update_send_or_appointment_dates(self):
  p=payload({'status':'signed','record_insert__datetime':'2026-10-01 10:00:00','record_update__datetime':'2026-10-02 11:00:00','sms_sended_date':'2026-10-03 12:00:00'})
  p['transaction']['transaction_head__datetime_appointment']='2026-10-04 13:00:00'
  self.assertEqual(sync.acceptance_datetime(p),'')
  self.assertTrue(sync.dashboard_dates({'payload_json':json.dumps(p)})['request_date_missing'])
 def test_only_service_offer_explicit_signed_date_is_used(self):
  p=payload({'status':'signed','signed_at':'2026-10-02 11:00:00'})
  p['sms_or_qrcode_sended']['statement_of_agreement']={'status':'signed','signed_at':'2026-01-01'}
  self.assertEqual(sync.acceptance_datetime(p),'2026-10-02T11:00:00+03:00')
  p['sms_or_qrcode_sended']['service_offer']['signed_at']='2026-10-26T10:00:00Z'
  self.assertEqual(sync.acceptance_datetime(p),'2026-10-26T12:00:00+02:00')
 def test_date_only_is_not_given_an_invented_time_and_conflicts_are_unknown(self):
  p=payload({'status':'signed','accepted_at':'2026-10-02'})
  self.assertEqual(sync.acceptance_datetime(p),'2026-10-02')
  p['sms_or_qrcode_sended']['service_offer']['signed_at']='2026-10-03'
  self.assertEqual(sync.acceptance_datetime(p),'')
  p=payload({'status':'sent','signed_at':'2026-10-02'})
  self.assertEqual(sync.acceptance_datetime(p),'')
 def test_date_projection_keeps_import_time_out_of_acceptance(self):
  data=item();data['detail']['accepted_at']='2026-10-02T11:00:00+03:00'
  sync.apply_snapshot(self.db,[data]);record=sync.offer_record(self.db,1)
  self.assertEqual(sync.dashboard_dates(record)['request_datetime'],data['detail']['accepted_at'])
  self.assertIn('2026-10-02T11:00:00+03:00',sync.offer_html(record,{'status':'acceptat'}))
  data=item();sync.apply_snapshot(self.db,[data]);record=sync.offer_record(self.db,1)
  self.assertEqual(sync.dashboard_dates(record)['request_datetime'],'')
  self.assertIn('indisponibilă în DSM',sync.offer_html(record,{'status':'acceptat'}))
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.db=self.root/'requests.db'
  with patch.object(database,'DB_PATH',self.db):database.init_db()
  gc.collect()
  with sync.connection(self.db) as conn:
   conn.execute('CREATE TABLE scheduler_appointments(id INTEGER PRIMARY KEY,request_id INTEGER,job_number TEXT,status TEXT)')
   conn.execute('CREATE TABLE request_archive(request_id INTEGER PRIMARY KEY)')
 def test_only_actual_service_offer_signature_counts(self):
  for state in ('signed','complete','completed',{'record_status__code':'signed'}):
   self.assertEqual(sync.eligibility(payload(state)),'accepted')
  for state in ('update','sent','insert','',None):self.assertEqual(sync.eligibility(payload(state)),'unsigned')
  p=payload();p['sms_or_qrcode_sended']={'statement_of_agreement':'signed'}
  self.assertEqual(sync.eligibility(p),'unsigned')
 def test_scheduled_converted_and_closed_offers_not_waiting(self):
  for status in ('0','1'):
   self.assertEqual(sync.eligibility(payload(children=[{'transaction_head__type':'3','transaction_status':status}])),'scheduled')
  self.assertEqual(sync.eligibility(payload(children=[{'transaction_head__type':'2','transaction_status':'0'}])),'converted')
  self.assertEqual(sync.eligibility(payload(status='completed')),'closed')
  self.assertEqual(sync.eligibility(payload(children=[{'transaction_head__type':'3','transaction_status':'4'}])),'accepted')
 def test_daily_reimport_has_stable_id_and_preserves_local_cancel_and_archive(self):
  first=sync.apply_snapshot(self.db,[item()]);ident=first['request_ids'][0]
  self.assertEqual(sync.apply_snapshot(self.db,[item()])['imported'],0)
  with sync.connection(self.db) as conn:
   conn.execute("UPDATE service_requests SET status='anulat' WHERE id=?",(ident,));conn.execute('INSERT INTO request_archive VALUES (?)',(ident,))
  sync.apply_snapshot(self.db,[item()])
  with sync.connection(self.db) as conn:
   self.assertEqual(conn.execute('SELECT status FROM service_requests').fetchall(),[('anulat',)])
   self.assertEqual(conn.execute('SELECT request_id FROM request_archive').fetchall(),[(ident,)])
 def test_existing_matching_calendar_entry_is_linked_not_recreated(self):
  with sync.connection(self.db) as conn:conn.execute("INSERT INTO scheduler_appointments VALUES (1,NULL,'OFERTA-1','programat')")
  result=sync.apply_snapshot(self.db,[item()])
  with sync.connection(self.db) as conn:
   self.assertEqual(conn.execute('SELECT status FROM service_requests').fetchone()[0],'programat')
   self.assertEqual(conn.execute('SELECT request_id FROM scheduler_appointments').fetchone()[0],result['request_ids'][0])
 def test_invalid_batch_rolls_back_every_insert(self):
  invalid=item('2');invalid['detail']['transaction_id']='99'
  with self.assertRaises(ValueError):sync.apply_snapshot(self.db,[item(),invalid])
  with sync.connection(self.db) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0],0)
 def test_revoked_signature_removed_from_waiting_and_resigning_restores_it(self):
  sync.apply_snapshot(self.db,[item()]);revoked=item();revoked.update(reason='unsigned',signature='update')
  sync.apply_snapshot(self.db,[revoked])
  with sync.connection(self.db) as conn:self.assertEqual(conn.execute('SELECT status FROM service_requests').fetchone()[0],'ofertat')
  sync.apply_snapshot(self.db,[item()])
  with sync.connection(self.db) as conn:self.assertEqual(conn.execute('SELECT status FROM service_requests').fetchone()[0],'acceptat')
 def test_all_imported_offers_visible_even_outside_recent_fifty(self):
  sync.apply_snapshot(self.db,[item()])
  with sync.connection(self.db) as conn:
   conn.executemany("INSERT INTO service_requests(session_id,symptom,source) VALUES (?,'fixture','whatsapp')",[(str(x),) for x in range(60)])
  with patch.object(database,'DB_PATH',self.db):rows=database.list_requests(include_dsm_accepted=True)
  gc.collect()
  self.assertEqual(len(rows),51);self.assertTrue(any(r['source']=='dsm_accepted_offer' for r in rows))
 def test_daily_clock_uses_bucharest_and_survives_dst(self):
  for day in (19,26):
   now=datetime(2026,10,day,7,0,tzinfo=sync.ROMANIA)
   self.assertTrue(sync.due({'enabled':True},now))
   self.assertFalse(sync.due({'last_success_day':now.date().isoformat()},now))
   self.assertFalse(sync.due({'enabled':False},now))
   self.assertFalse(sync.due({},now.replace(hour=6,minute=59)))
 def test_signed_quote_page_keeps_agreed_prices_and_escapes_customer_data(self):
  data=item();data['detail']['customer_name']='<script>bad()</script>'
  sync.apply_snapshot(self.db,[data]);record=sync.offer_record(self.db,1)
  page=sync.offer_html(record,{'status':'acceptat'})
  self.assertIn('&lt;script&gt;',page);self.assertNotIn('<script>bad()',page)
  self.assertIn('P1',page);self.assertIn('100',page);self.assertIn('200',page)
  self.assertNotIn('send-whatsapp',page)
 def test_failed_dsm_read_preserves_database_and_does_not_mark_success(self):
  sync.apply_snapshot(self.db,[item()])
  with patch.object(sync,'read_snapshot',AsyncMock(side_effect=RuntimeError('Autentificare DSM necesară'))):
   result=asyncio.run(sync.synchronize(self.db,self.root))
  self.assertFalse(result['ok']);self.assertNotIn('last_success_day',sync.state(self.root))
  with sync.connection(self.db) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0],1)

 def test_rebuild_removes_old_quotes_but_preserves_new_request_and_calendar(self):
  signed=sync.apply_snapshot(self.db,[item()])['request_ids'][0]
  with sync.connection(self.db) as conn:
   duplicate=conn.execute("INSERT INTO service_requests(session_id,registration,status,source,symptom) VALUES ('old','B100TST','ofertat','dashboard','Plăcuțe')").lastrowid
   new=conn.execute("INSERT INTO service_requests(session_id,registration,status,source,symptom) VALUES ('new','B110TST','nou','whatsapp','Cerere nouă')").lastrowid
   conn.execute("INSERT INTO scheduler_appointments VALUES (3,?,'EXISTING','programat')",(duplicate,))
   before=conn.execute('SELECT * FROM scheduler_appointments').fetchall()
  result=sync.rebuild_dashboard(self.db,self.root,[item()])
  self.assertEqual(result['request_ids'],[signed]);self.assertEqual(result['archived_ids'],[duplicate])
  self.assertIn(new,result['untouched_ids']);self.assertTrue((Path(result['backup'])/'requests.sqlite3').is_file())
  with sync.connection(self.db) as conn:
   self.assertEqual(conn.execute('SELECT COUNT(*) FROM service_requests').fetchone()[0],3)
   self.assertEqual(conn.execute('SELECT * FROM scheduler_appointments').fetchall(),before)

 def test_rebuild_keeps_different_signed_offers_for_same_plate_and_restores_canonical(self):
  initial=sync.apply_snapshot(self.db,[item()])['request_ids'][0]
  with sync.connection(self.db) as conn:conn.execute('INSERT INTO request_archive VALUES (?)',(initial,))
  result=sync.rebuild_dashboard(self.db,self.root,[item(),item('2','OFERTA-2')])
  self.assertEqual(result['accepted'],2);self.assertEqual(result['restored'],1)
  again=sync.rebuild_dashboard(self.db,self.root,[item(),item('2','OFERTA-2')])
  self.assertEqual(again['request_ids'],result['request_ids']);self.assertEqual(again['sync']['imported'],0)

 def test_rebuild_dsm_read_failure_does_not_hide_existing_cards(self):
  sync.apply_snapshot(self.db,[item()])
  with patch.object(sync,'read_snapshot',AsyncMock(side_effect=RuntimeError('DSM login'))):
   with self.assertRaises(RuntimeError):asyncio.run(sync.rebuild_signed_offers(self.db,self.root))
  with sync.connection(self.db) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM request_archive').fetchone()[0],0)

if __name__=='__main__':unittest.main()
