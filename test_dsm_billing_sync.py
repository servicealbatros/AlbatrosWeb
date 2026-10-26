import ast
import gc
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import database
import dsm_billing_sync as billing
from dsm_accepted_offers import connection, reconcile_existing_bookings
from scheduler import init_scheduler_db

VIN = 'WV100000000000008'


def details():
    return {
        '10': dict(transaction=dict(transaction_head__id=10, transaction_head__type='1',
            transaction_head__code='OFERTA-10', transaction_head__status='completed', car_body_series=VIN),
            transaction_head_childs=[dict(transaction_id='11', transaction_head__type='2', transaction_status='3')]),
        '11': dict(transaction=dict(transaction_head__id=11, transaction_head__type='2',
            transaction_head__code='PHDEV-11', transaction_head__status='billed', car_body_series=VIN,
            parent_transaction_code='OFERTA-10', parent_transaction_type='1'), transaction_head_childs=[]),
    }


class BillingSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup); self.addCleanup(gc.collect)
        self.db = Path(self.temp.name)/'test.db'
        with patch.object(database, 'DB_PATH', self.db):
            database.init_db()
        init_scheduler_db(self.db)
        with connection(self.db) as conn:
            conn.execute("INSERT INTO service_requests(id,session_id,vin,registration,source,status,symptom) VALUES (57,'fixture',?,'B112TST','whatsapp','programat','Revizie')", (VIN,))
            conn.execute('CREATE TABLE dsm_booking_approvals(request_id INTEGER PRIMARY KEY,offer_id TEXT,payload_json TEXT)')
            conn.execute('INSERT INTO dsm_booking_approvals VALUES (57,?,?)', ('10',json.dumps(dict(vin=VIN,offer_code='OFERTA-10',order_id='11'))))
            conn.execute("""INSERT INTO scheduler_appointments(request_id,start_at,end_at,status,registration,source,dsm_appointment_id,operations,estimated_hours)
                VALUES (57,'2026-11-01T08:30','2026-11-01T10:30','programat','B112TST','dsm_schedule_readonly','APP-1','Revizie',2)""")

    def statuses(self):
        with connection(self.db) as conn:
            return (conn.execute('SELECT status FROM service_requests WHERE id=57').fetchone()[0],
                    conn.execute('SELECT status FROM scheduler_appointments WHERE request_id=57').fetchone()[0])

    async def run_sync(self, payloads):
        async def read(ids):
            return {i:payloads[i] for i in ids if i in payloads}
        with patch.object(billing, '_read_details', AsyncMock(side_effect=read)):
            return await billing.reconcile_billed_requests(self.db)

    async def test_billed_exact_order_resolves_request_finishes_calendar_and_is_idempotent(self):
        result = await self.run_sync(details())
        self.assertEqual(result['resolved_request_ids'], [57])
        self.assertEqual(self.statuses(), ('rezolvat','finalizat'))
        self.assertEqual(reconcile_existing_bookings(self.db), [])
        self.assertEqual((await self.run_sync(details()))['resolved_request_ids'], [])
        from dsm_schedule_sync import sync_local_schedule
        tomorrow=(datetime.now()+timedelta(days=1)).date().isoformat()
        sync_local_schedule(self.db,[dict(dsm_appointment_id='APP-1',registration='B112TST',
            operations='OFERTA 10-Revizie',start_at=tomorrow+'T08:30',end_at=tomorrow+'T10:30')])
        self.assertEqual(self.statuses(), ('rezolvat','finalizat'))
        with connection(self.db) as conn:
            proof=json.loads(conn.execute('SELECT payload_json FROM dsm_billing_reconciliations').fetchone()[0])
        self.assertEqual(proof['order_codes'], ['PHDEV-11'])
        import app
        row=dict(id=57,source='whatsapp',status='rezolvat')
        with patch.object(app, 'DB_PATH', self.db), patch.object(app,'list_requests',return_value=[row]), \
             patch.object(app,'_archive_ids',return_value=set()),patch.object(app,'REQUEST_STATUS',{57:'programat'}), \
             patch.object(app,'save_request_status'),patch.object(app,'_load_request_offer_result',return_value={}), \
             patch.object(app,'_draft_for_request',return_value={}):
            self.assertEqual(app.requests()[0]['desktop_status'],'rezolvat')

    async def test_closed_exact_order_without_invoice_also_resolves(self):
        binding=billing.bindings(self.db)[0]
        for status in ('completed','closed'):
            p=details();p['11']['transaction']['transaction_head__status']=status
            with self.subTest(status=status):
                proof=billing.billed_evidence(binding,p)
                self.assertEqual(proof['order_statuses'],{'11':status})
        p=details();p['11']['transaction']['transaction_head__status']='completed'
        self.assertEqual((await self.run_sync(p))['resolved_request_ids'],[57])
        self.assertEqual(self.statuses(),('rezolvat','finalizat'))

    async def test_open_cancelled_wrong_vin_and_unrelated_orders_do_not_resolve(self):
        cases=[]
        for field,value in [('transaction_head__status','working'),('transaction_head__status','unknown'),
            ('transaction_head__status','canceled'),('transaction_head__status','cancelled'),('car_body_series','WBA00000000000002'),
            ('parent_transaction_code','OFERTA-OLD'),('parent_transaction_type','2')]:
            p=details();p['11']['transaction'][field]=value;cases.append(p)
        p=details();p['10']['transaction']['car_body_series']='WBA00000000000002';cases.append(p)
        p=details();p['10']['transaction']['transaction_head__code']='OFERTA-OLD';cases.append(p)
        for p in cases:
            self.assertEqual((await self.run_sync(p))['resolved_request_ids'],[])
            self.assertEqual(self.statuses(),('programat','programat'))

    async def test_split_order_stays_open_until_all_orders_are_closed_or_billed(self):
        p=details()
        p['10']['transaction_head_childs'].append(dict(transaction_id='12',transaction_head__type='2',transaction_status='0'))
        p['12']=json.loads(json.dumps(p['11']))
        p['12']['transaction'].update(transaction_head__id=12,transaction_head__code='PHDEV-12',transaction_head__status='working')
        self.assertEqual((await self.run_sync(p))['resolved_request_ids'],[])
        p['12']['transaction']['transaction_head__status']='completed'
        self.assertEqual((await self.run_sync(p))['resolved_request_ids'],[57])

    async def test_cancelled_request_is_preserved_and_disconnection_does_not_resolve(self):
        with patch.object(billing,'_read_details',AsyncMock(side_effect=RuntimeError('Autentificare DSM necesară'))):
            with self.assertRaisesRegex(RuntimeError,'Autentificare'):
                await billing.reconcile_billed_requests(self.db)
        self.assertEqual(self.statuses(),('programat','programat'))
        with connection(self.db) as conn:conn.execute("UPDATE service_requests SET status='anulat'")
        self.assertEqual((await self.run_sync(details()))['checked'],0)
        self.assertEqual(self.statuses(),('anulat','programat'))

    async def test_imported_quote_does_not_use_an_older_invoice_for_same_vin(self):
        from dsm_accepted_offers import init_links
        with connection(self.db) as conn:
            conn.execute('DROP TABLE dsm_booking_approvals')
            init_links(conn)
            conn.execute('INSERT INTO dsm_accepted_offer_links VALUES (?,?,?,?,?,?,?)',
                ('10',57,'OFERTA-10','signed','accepted',json.dumps(dict(vin=VIN,document_code='OFERTA-10')),'2026-10-26'))
        p=details();p['10']['transaction_head_childs']=[]
        self.assertEqual((await self.run_sync(p))['resolved_request_ids'],[])
        p=details()
        self.assertEqual((await self.run_sync(p))['resolved_request_ids'],[57])

    async def test_import_calls_billing_without_dashboard_being_open(self):
        tree=ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8-sig'))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_import_dsm_rolling_week')
        env=dict(datetime=datetime,timedelta=timedelta,DB_PATH=self.db,
                 import_dsm_schedule=AsyncMock(return_value=dict(captured=1,inserted=0,updated=1)))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'app.py','exec'),env)
        with patch.object(billing,'reconcile_billed_requests',AsyncMock(return_value=dict(checked=1,resolved_request_ids=[57]))) as sync:
            result=await env['_import_dsm_rolling_week']()
        self.assertTrue(result['ok']);self.assertEqual(result['billing']['resolved_request_ids'],[57])
        self.assertEqual(env['import_dsm_schedule'].await_count,8)
        sync.assert_awaited_once_with(self.db)

    async def test_detail_reader_rejects_wrong_ids_or_partial_reads(self):
        for returned in ([],[details()['11']]):
            with patch('dsm_open_orders_readonly._evaluate',AsyncMock(return_value=returned)):
                with self.assertRaises(ValueError):await billing._read_details(['10'])
        self.assertEqual(self.statuses(),('programat','programat'))


if __name__=='__main__':
    unittest.main()
