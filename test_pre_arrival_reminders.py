import gc
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import AsyncMock,patch
import database
from scheduler import init_scheduler_db
from dsm_accepted_offers import connection
import pre_arrival_reminders as reminders
import pre_reception as flow

VIN='WV100000000000008'
PHONE='40700100009'


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.addCleanup(gc.collect)
        self.root=Path(self.temp.name);self.db=self.root/'db.sqlite3'
        with patch.object(database,'DB_PATH',self.db):database.init_db()
        init_scheduler_db(self.db)
        self.now=(datetime.now(reminders.ROMANIA)+timedelta(days=1)).replace(hour=17,minute=0,second=0,microsecond=0)
        self.target=(self.now+timedelta(days=1)).date()
        reminders.configure(self.root,True,self.now.date().isoformat(),['B112TST'])
        with connection(self.db) as conn:
            conn.execute("""INSERT INTO scheduler_appointments(id,start_at,end_at,status,registration,operations,estimated_hours,dsm_appointment_id)
                VALUES (1,?,?,'programat','B112TST','Revizie',2,'APP-1')""",(str(self.target)+'T08:30',str(self.target)+'T10:30'))
        self.data=dict(vin=VIN,registration='B112TST',phone=PHONE,client_name='Client',
                       client_address='Ploiesti Str. Test 1',work='Revizie',previous_dsm={})
        from pre_arrival_policy import evaluate
        self.data['document_policy']=evaluate(vehicle_exists=True,last_visit='2000-01-01',checked_on=self.now.date())
        self.seed=AsyncMock(return_value=self.data.copy())
        self.notify=AsyncMock(return_value={'messages':[{'id':'wamid.fixture'}]})

    async def tick(self,now=None):
        return await reminders.tick(self.db,self.root,now or self.now,self.notify,self.seed)

    async def test_starts_tomorrow_at_17_and_sends_once_after_restart(self):
        self.assertIsNone(await self.tick(self.now-timedelta(days=1)))
        self.assertIsNone(await self.tick(self.now-timedelta(minutes=1)))
        self.assertFalse(reminders.due(self.root,self.now+timedelta(hours=1)))
        self.assertEqual((await self.tick())['sent'],1)
        self.assertEqual((await self.tick())['skipped'],1)
        self.assertEqual(self.notify.await_count,1)
        stage,data=flow.get(self.root,PHONE)
        self.assertEqual(stage,'attendance');self.assertTrue(data['production'])
        self.assertIn('ALBATROS A',self.notify.await_args.args[1]);self.assertNotIn('TEST',self.notify.await_args.args[1])
        self.assertEqual(reminders.status(self.root)['counts'],{'sent':1})

    async def test_only_selected_cars_can_be_contacted_and_configuration_survives_stop(self):
        reminders.configure(self.root,False,self.now.date().isoformat(),['b114 tst','B115TST','B116TST'])
        self.assertEqual(reminders.settings(self.root)['registrations'],['B114TST','B115TST','B116TST'])
        reminders.configure(self.root,True,self.now.date().isoformat())
        self.assertFalse(reminders.due(self.root,self.now+timedelta(days=1)))
        self.assertEqual((await self.tick())['sent'],0);self.seed.assert_not_awaited();self.notify.assert_not_awaited()
        self.assertEqual((await reminders.preview(self.db,self.root,self.target,self.seed))['eligible'],0)
        with self.assertRaises(ValueError):reminders.configure(self.root,True,self.now.date().isoformat(),[])

    async def test_existing_work_order_excludes_customer_from_test_and_prevents_invitation(self):
        check=AsyncMock(return_value=[{'id':'20','code':'PHDEV-20'}])
        preview=await reminders.preview(self.db,self.root,self.target,self.seed,check_orders=check)
        self.assertEqual(preview['eligible'],0);self.assertEqual(preview['candidates'][0]['existing_orders'][0]['code'],'PHDEV-20')
        await reminders.tick(self.db,self.root,self.now,self.notify,self.seed,check_orders=check)
        self.notify.assert_not_awaited();self.assertIn('duplicat',reminders.status(self.root)['issues'][0]['error'])

    async def test_explicit_existing_order_binding_allows_invitation_only_for_that_order(self):
        reminders.configure(self.root,True,self.now.date().isoformat(),['B112TST'],{'B112TST':'20'})
        reminders.configure(self.root,True,self.now.date().isoformat())
        check=AsyncMock(return_value=[{'id':'20','code':'PHDEV-20'}])
        result=await reminders.preview(self.db,self.root,self.target,self.seed,check_orders=check)
        self.assertEqual(result['eligible'],1);self.assertEqual(result['candidates'][0]['mode'],'update_existing')
        await reminders.tick(self.db,self.root,self.now,self.notify,self.seed,check_orders=check)
        data=flow.get(self.root,PHONE)[1]
        self.assertEqual(data['appointment_command_id'],'20');self.assertEqual(data['approved_existing_order_id'],'20')
        check.return_value=[]
        result=await reminders.preview(self.db,self.root,self.target,self.seed,check_orders=check)
        self.assertEqual(result['eligible'],0)
        check.return_value=[{'id':'21','code':'PHDEV-21'}]
        result=await reminders.preview(self.db,self.root,self.target,self.seed,check_orders=check)
        self.assertEqual(result['eligible'],0)
        reminders.configure(self.root,False,self.now.date().isoformat(),[])
        self.assertEqual(reminders.settings(self.root)['existing_order_ids'],{})

    async def test_removed_dsm_appointment_is_excluded_by_fresh_snapshot_after_restart(self):
        refresh=AsyncMock(return_value={'appointment_ids':[]})
        result=await reminders.tick(self.db,self.root,self.now,self.notify,self.seed,refresh=refresh)
        self.assertEqual(result['sent'],0);self.notify.assert_not_awaited()
        result=await reminders.tick(self.db,self.root,self.now,self.notify,self.seed,refresh=refresh)
        self.assertEqual(result['sent'],0);self.assertEqual(refresh.await_count,1)

    async def test_uncertain_delivery_and_unidentified_client_are_not_retried_blindly(self):
        self.notify.side_effect=TimeoutError('Rezultat incert')
        self.assertEqual((await self.tick())['failed'],1)
        await self.tick();self.assertEqual(self.notify.await_count,1)
        self.assertEqual(reminders.status(self.root)['counts'],{'uncertain':1})

    async def test_cancelled_during_lookup_and_continuations_are_not_messaged(self):
        async def seed(row):
            with connection(self.db) as conn:conn.execute("UPDATE scheduler_appointments SET status='anulat'")
            return self.data.copy()
        self.seed.side_effect=seed
        await self.tick();self.notify.assert_not_awaited()
        with connection(self.db) as conn:conn.execute("UPDATE scheduler_appointments SET status='programat',continued_from_id=99")
        self.assertEqual(reminders.appointments(self.db,self.target),[])

    async def test_missing_phone_is_visible_for_adviser_without_sending(self):
        self.seed.side_effect=ValueError('Lipsește un număr mobil unic și valid al clientului.')
        self.assertEqual((await self.tick())['failed'],1)
        self.notify.assert_not_awaited()
        self.assertEqual(reminders.status(self.root)['issues'][0]['appointment_id'],1)

    async def test_import_preserves_source_identity_and_excludes_cancelled_appointments(self):
        from dsm_schedule_sync import sync_local_schedule
        row=dict(dsm_appointment_id='APP-1',registration='B112TST',operations='Revizie',
            start_at=str(self.target)+'T08:30',end_at=str(self.target)+'T10:30',
            source_transaction_id='19631',source_transaction_type='3',status='anulat')
        sync_local_schedule(self.db,[row])
        with connection(self.db) as conn:
            self.assertEqual(conn.execute('SELECT dsm_transaction_id,dsm_transaction_type,status FROM scheduler_appointments').fetchone(),('19631','3','anulat'))
        self.assertEqual(reminders.appointments(self.db,self.target),[])

    async def test_fresh_dsm_read_is_required_and_stop_safe_prevents_sending(self):
        refresh=AsyncMock(side_effect=RuntimeError('DSM neconectat'))
        await reminders.tick(self.db,self.root,self.now,self.notify,self.seed,refresh=refresh)
        self.notify.assert_not_awaited()
        self.assertIn('DSM',reminders.status(self.root)['last_refresh_error'])
        refresh.side_effect=None
        await reminders.tick(self.db,self.root,self.now,self.notify,self.seed,refresh=refresh,allowed=lambda:False)
        self.assertEqual(refresh.await_count,1)
        await reminders.tick(self.db,self.root,self.now,self.notify,self.seed,refresh=refresh)
        self.assertEqual(self.notify.await_count,1)
        self.assertEqual(reminders.status(self.root)['last_refresh_day'],self.now.date().isoformat())

    async def test_production_client_completes_document_checks_phone_and_final_consent(self):
        await self.tick()
        send=AsyncMock();writer=AsyncMock(return_value=dict(id='99',code='PHDEV-99',itp='2035-01-01'))
        await flow.text(self.root,PHONE,'DA',send,writer)
        parsed=dict(vin=VIN,registration='B112TST',owner_name='Client Test',manufacture_year=2020,
                    itp_expires='2035-01-01',itp_evidence='ITP 01.01.2035')
        await flow.image(self.root,PHONE,{'image':{'id':'document'}},send,
                         AsyncMock(return_value=(b'image','image/jpeg')),lambda *_:parsed)
        for expected in ('document_itp','document_year','requirements'):
            await flow.text(self.root,PHONE,'DA',send,writer)
            self.assertEqual(flow.get(self.root,PHONE)[0],expected)
        writer.assert_not_awaited()
        await flow.text(self.root,PHONE,'NU',send,writer)
        self.assertEqual(flow.get(self.root,PHONE)[0],'phone')
        await flow.text(self.root,PHONE,'DA',send,writer)
        self.assertEqual(flow.get(self.root,PHONE)[0],'confirm')
        writer.assert_not_awaited()
        await flow.text(self.root,PHONE,'CONFIRM DATELE',send,writer)
        self.assertEqual(flow.get(self.root,PHONE)[0],'done')
        self.assertTrue(writer.await_args.args[0]['phone_confirmed'])
        await flow.text(self.root,PHONE,'CONFIRM DATELE',send,writer)
        self.assertEqual(writer.await_count,1)
        self.assertTrue(all('TEST' not in c.args[1] and 'DSM' not in c.args[1] for c in send.await_args_list))
        self.assertFalse(await flow.text(self.root,PHONE,'O altă întrebare',send,writer))

    async def test_decline_does_not_cancel_booking_and_stop_is_persisted(self):
        await self.tick();send=AsyncMock();writer=AsyncMock()
        await flow.text(self.root,PHONE,'NU',send,writer)
        with connection(self.db) as conn:self.assertEqual(conn.execute('SELECT status FROM scheduler_appointments').fetchone()[0],'programat')
        writer.assert_not_awaited()
        await flow.text(self.root,PHONE,'STOP',send,writer)
        self.assertTrue(flow.get(self.root,PHONE)[1]['production_optout'])


if __name__=='__main__':unittest.main()
