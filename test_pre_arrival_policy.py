import copy
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

import pre_arrival_policy as policy
import pre_arrival_reminders as reminders
import pre_reception as flow
import dsm_pre_arrival as dsm

TODAY = date(2026, 10, 30)
VIN = 'WV100000000000008'
PHONE = '40700100009'


def data(last_visit='2026-08-10'):
    decision = dict(policy.evaluate(vehicle_exists=True,last_visit=last_visit,checked_on=TODAY),
                    vin=VIN,car_id='22',visit_order_id='99')
    return dict(production=True,vin=VIN,car_id='22',contact_id='33',registration='B112TST',
                phone=PHONE,client_name='Client Test',client_address='Ploiesti, Str. Test 1',
                previous_dsm=dict(car_itp_expires_date='2030-01-01',car_manufacture_year='2020'),
                document_policy=decision,offer_id='arrival:test',expires_at='2099-01-01',
                work='Revizie',appointment_start='2030-01-01T08:30',
                source_transaction_id='10',source_type='2',source_code='PHDEV-10',source_explicit=True)


def head(ident='99',visit='2026-08-10',kind='2',status='billed',car_id='22',vin=VIN):
    return dict(transaction_head__id=ident,transaction_head__type=kind,transaction_head__status=status,
                transaction_head__code='PHDEV-'+ident,car_id=car_id,contact_id='33',car_body_series=vin,
                car_auto_number='B112TST',transaction_head__datetime_appointment=visit)


class PolicyTests(unittest.TestCase):
    def test_calendar_year_boundary_and_first_visit(self):
        for visit,expected in [('2025-10-31','reuse_dsm'),('2025-10-30','request_document'),
                               ('2025-10-29','request_document')]:
            self.assertEqual(policy.evaluate(vehicle_exists=True,last_visit=visit,checked_on=TODAY)['decision'],expected)
        self.assertEqual(policy.evaluate(vehicle_exists=False,checked_on=TODAY)['reason'],'vehicle_not_found')

    def test_leap_anniversary_is_a_calendar_year(self):
        self.assertEqual(policy.anniversary(date(2024,2,29)),date(2025,2,28))
        self.assertEqual(policy.evaluate(vehicle_exists=True,last_visit='2024-03-01',checked_on=date(2025,2,28))['decision'],'reuse_dsm')

    def test_unknown_future_or_unverified_visit_does_not_invent_an_annual_update(self):
        for visit in (None,'invalid','2026-10-31'):
            self.assertEqual(policy.evaluate(vehicle_exists=True,last_visit=visit,checked_on=TODAY)['decision'],'review')
        self.assertEqual(policy.evaluate(vehicle_exists=True,last_visit='2026-08-10',checked_on=TODAY,history_complete=False)['decision'],'review')

    def test_snapshot_is_bound_to_vehicle_and_expires_on_the_anniversary(self):
        value=data('2025-10-31')
        self.assertTrue(policy.can_reuse(value,checked_on=TODAY))
        self.assertFalse(policy.can_reuse(value,checked_on=date(2026,10,31)))
        for key in ('vin','car_id'):
            altered=copy.deepcopy(value);altered[key]='different'
            self.assertFalse(policy.can_reuse(altered,checked_on=TODAY))
        value['manual_test']=True
        self.assertFalse(policy.can_reuse(value,checked_on=TODAY))

    def test_saved_dates_use_romanian_day_and_common_dsm_formats(self):
        self.assertEqual(policy.saved_date('30.10.2026 08:30'),TODAY)
        self.assertEqual(policy.saved_date('2026-10-29T23:30:00+00:00'),TODAY)


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def read(self,heads,refs=None):
        refs=refs if refs is not None else [dict(transaction_id=h['transaction_head__id'],transaction_type=h['transaction_head__type'],
                                               status=h['transaction_head__status']) for h in heads]
        details={h['transaction_head__id']:{'transaction':h} for h in heads}
        with patch.object(dsm,'_search_transactions',AsyncMock(return_value=refs)), \
                patch.object(dsm,'_read_details',AsyncMock(return_value=details)) as read:
            result=await dsm.document_requirements(data(),checked_on=TODAY)
        return result,read

    async def test_latest_completed_order_controls_recency_not_offers_or_new_open_orders(self):
        result,read=await self.read([head('1','2025-10-20'),head('2','2026-10-29',kind='1'),
                                    head('3','2026-10-29',kind='3'),head('4','2026-10-29',status='working'),
                                    head('5','2026-10-29',status='canceled')])
        self.assertEqual(result['decision'],'request_document')
        self.assertEqual(result['visit_order_id'],'1')
        self.assertEqual(read.await_args.args[0],['1','4'])

    async def test_another_car_with_same_plate_cannot_supply_the_last_visit(self):
        result,_=await self.read([head('1','2025-01-01'),head('2','2026-10-29',car_id='77',vin='WBA00000000000002')])
        self.assertEqual(result['last_visit'],'2025-01-01')
        with self.assertRaisesRegex(ValueError,'VIN'):
            await self.read([head(vin='WBA00000000000002')])
        result,_=await self.read([head('1','2025-01-01'),head('2','2026-10-29',car_id='77')])
        self.assertEqual(result['decision'],'reuse_dsm')
        self.assertEqual(result['last_visit'],'2026-10-29')

    async def test_missing_or_incomplete_history_requires_review_without_requesting_photos(self):
        for heads in ([],[head(visit='invalid')],[head(visit='2026-11-01')]):
            result,_=await self.read(heads)
            self.assertEqual(result['decision'],'review')
        with self.assertRaisesRegex(ValueError,'incomplet'):
            await self.read([],refs=[dict(transaction_id='99',transaction_type='2',status='billed')])

    async def test_verified_recent_visit_is_enough_even_when_an_older_date_is_missing(self):
        result,_=await self.read([head('1',visit='invalid'),head('2',visit='2026-08-10')])
        self.assertEqual(result['decision'],'reuse_dsm')

    async def test_unknown_vehicle_requests_document_without_guessing_history(self):
        value=data();value.update(vehicle_missing=True,vin='',car_id='')
        with patch.object(dsm,'_search_transactions',AsyncMock()) as search:
            result=await dsm.document_requirements(value,checked_on=TODAY)
        self.assertEqual(result['reason'],'vehicle_not_found')
        search.assert_not_awaited()

    async def test_native_first_customer_can_receive_document_request_without_a_generic_vehicle(self):
        async def call(path,fields):
            self.assertIn('get_client',path)
            return dict(contact_name='Client Test',mobile=[dict(val=PHONE,checked='checked')],address=[])
        appointment=dict(status='success',detail=dict(clientId=33,carNumber='B112TST',clientPhone=PHONE,
                         canTransform=True,start='2030-01-01T08:30'))
        with patch.object(dsm,'appointment_call',AsyncMock(return_value=appointment)),patch.object(dsm,'call',AsyncMock(side_effect=call)):
            result=await dsm.seed(dict(registration='B112TST',dsm_transaction_id='10',dsm_transaction_type='3'))
        self.assertTrue(result['vehicle_missing'])
        self.assertEqual(result['document_policy']['reason'],'vehicle_not_found')


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        patcher=patch.object(policy,'today',return_value=TODAY);patcher.start();self.addCleanup(patcher.stop)
        self.send=AsyncMock();self.writer=AsyncMock(return_value=dict(id='10',code='PHDEV-10',itp='2030-01-01',data_source='dsm'))

    async def text(self,value,**kwargs):
        return await flow.text(self.root,PHONE,value,self.send,self.writer,**kwargs)

    async def test_recent_customer_reuses_saved_data_without_ocr_and_opens_only_after_confirmation(self):
        value=data();flow.save(self.root,PHONE,'attendance',value)
        await self.text('DA')
        stage,saved=flow.get(self.root,PHONE)
        self.assertEqual(stage,'phone');self.assertEqual(saved['document_source'],'dsm')
        self.assertNotIn('document_verified',saved)
        for message,expected in [('DA','requirements'),('NU','confirm')]:
            await self.text(message);self.assertEqual(flow.get(self.root,PHONE)[0],expected)
            self.writer.assert_not_awaited()
        await self.text('CONFIRM DATELE')
        self.assertEqual(flow.get(self.root,PHONE)[0],'done')
        self.assertEqual(self.writer.await_args.args[0]['itp'],'2030-01-01')
        self.assertFalse(any('fotograf' in item.args[1].lower() for item in self.send.await_args_list))
        self.assertTrue(all('DSM' not in item.args[1] for item in self.send.await_args_list))
        await self.text('CONFIRM DATELE');self.writer.assert_awaited_once()

    async def test_recent_vehicle_with_missing_itp_or_year_uses_only_existing_fields(self):
        value=data();value['previous_dsm']={}
        flow.save(self.root,PHONE,'attendance',value)
        for message in ('DA','DA','NU'):
            await self.text(message)
        saved=flow.get(self.root,PHONE)[1]
        self.assertIsNone(saved['itp']);self.assertIsNone(saved['manufacture_year'])
        self.assertIn('neînregistrat',self.send.await_args.args[1])

    async def test_anniversary_and_unknown_vehicle_request_photos(self):
        for value in (data('2025-10-30'),dict(data(),document_policy=policy.evaluate(vehicle_exists=False,checked_on=TODAY))):
            flow.save(self.root,PHONE,'attendance',value)
            await self.text('DA')
            self.assertEqual(flow.get(self.root,PHONE)[0],'document')
            self.assertIn('fotografie',self.send.await_args.args[1])
        self.writer.assert_not_awaited()

    async def test_unknown_history_or_read_failure_never_forces_a_document(self):
        value=data();value['document_policy']=policy.evaluate(vehicle_exists=True,checked_on=TODAY)
        flow.save(self.root,PHONE,'attendance',value)
        await self.text('DA')
        self.assertEqual(flow.get(self.root,PHONE)[0],'review')
        await self.text('CONFIRM DATELE')
        self.assertEqual(flow.get(self.root,PHONE)[0],'review')
        self.writer.assert_not_awaited()
        value=data();value.pop('document_policy')
        flow.save(self.root,PHONE,'attendance',value)
        await self.text('DA')
        self.assertEqual(flow.get(self.root,PHONE)[0],'review')
        value=data();flow.save(self.root,PHONE,'attendance',value)
        resolver=AsyncMock(side_effect=RuntimeError('DSM unavailable'))
        await self.text('DA',refresh_policy=resolver)
        self.assertEqual(flow.get(self.root,PHONE)[0],'review')
        resolver.side_effect=None;resolver.return_value=value['document_policy']
        await self.text('CONFIRM DATELE',refresh_policy=resolver)
        self.assertEqual(flow.get(self.root,PHONE)[0],'phone')

    async def test_old_pending_document_case_can_reuse_freshly_verified_dsm_data(self):
        value=data();decision=value.pop('document_policy')
        flow.save(self.root,PHONE,'document',value)
        await self.text('DA',refresh_policy=AsyncMock(return_value=decision))
        self.assertEqual(flow.get(self.root,PHONE)[0],'phone')
        self.assertEqual(flow.get(self.root,PHONE)[1]['document_source'],'dsm')

    async def test_first_customer_photo_must_match_plate_before_it_can_set_vin(self):
        value=data();value.update(vehicle_missing=True,vin='',car_id='')
        flow.save(self.root,PHONE,'document',value)
        reader=lambda *_:dict(vin=VIN,registration='B112TST',owner_name='Client Test',manufacture_year=2020)
        await flow.image(self.root,PHONE,dict(image=dict(id='fixture')),self.send,
                         AsyncMock(return_value=(b'fixture','image/jpeg')),reader)
        self.assertEqual(flow.get(self.root,PHONE)[0],'document_check')
        self.assertEqual(flow.get(self.root,PHONE)[1]['vin'],VIN)
        self.writer.assert_not_awaited()
        for parsed in (dict(vin='',registration='B112TST'),dict(vin=VIN,registration='B113TST')):
            flow.save(self.root,PHONE,'document',value)
            await flow.image(self.root,PHONE,dict(image=dict(id='rejected')),self.send,
                             AsyncMock(return_value=(b'fixture','image/jpeg')),lambda *_,parsed=parsed:parsed)
            self.assertEqual(flow.get(self.root,PHONE)[0],'document')
            self.assertFalse(flow.get(self.root,PHONE)[1].get('document_verified'))

    async def test_invitation_asks_for_photo_only_on_the_document_path(self):
        recent=data();recent['appointment_start']='2030-01-01T08:30'
        self.assertNotIn('trimiteți fotografii',reminders.invitation(recent))
        self.assertIn('trimiteți fotografii',reminders.invitation(data('2025-10-30')))


class ReminderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def tick(self,root,value,notify):
        now=datetime(2026,10,30,17,tzinfo=reminders.ROMANIA)
        reminders.configure(root,True,now.date().isoformat(),['B112TST'])
        appointment=dict(id=1,registration='B112TST',start_at=(now+timedelta(days=1)).date().isoformat()+'T08:30',
                         dsm_appointment_id='APP-1')
        with patch.object(policy,'today',return_value=TODAY), \
                patch.object(reminders,'current_appointments',return_value=[appointment]), \
                patch.object(reminders,'appointments',return_value=[appointment]):
            return await reminders.tick(Path(root)/'unused.db',root,now,notify,AsyncMock(return_value=value))

    async def test_recent_customer_invitation_and_policy_are_persisted_without_requesting_a_photo(self):
        with TemporaryDirectory() as root:
            notify=AsyncMock(return_value={'messages':[{'id':'fixture'}]})
            result=await self.tick(root,data(),notify)
            self.assertEqual(result['sent'],1)
            self.assertNotIn('trimiteți fotografii',notify.await_args.args[1])
            saved=flow.get(root,PHONE)
            self.assertEqual(saved[0],'attendance')
            self.assertEqual(saved[1]['document_policy']['decision'],'reuse_dsm')

    async def test_unverified_visit_goes_to_adviser_without_sending_an_automatic_photo_request(self):
        with TemporaryDirectory() as root:
            value=data();value['document_policy']=policy.evaluate(vehicle_exists=True,checked_on=TODAY)
            notify=AsyncMock()
            result=await self.tick(root,value,notify)
            self.assertEqual(result['failed'],1)
            notify.assert_not_awaited()
            self.assertEqual(reminders.status(root)['issues'][0]['state'],'review')
            self.assertNotIn('fotograf',reminders.invitation(dict(value,appointment_start='2030-01-01T08:30')))


class WriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        patcher=patch.object(policy,'today',return_value=TODAY);patcher.start();self.addCleanup(patcher.stop)

    async def test_recent_customer_gets_a_new_native_order_without_a_document_and_retry_reuses_it(self):
        value=data();policy.load_stored_vehicle(value)
        value.update(source_type='3',final_confirmed=True,phone_confirmed=True,extra='Nu')
        order=head('20',status='working')
        order['transaction_head__description']=dsm.requirements_description(value,'Revizie\n[ALBATROS PRE-RECEPTIE 10]')
        async def call(path,fields=None):
            if 'get_car' in path:return dict(car_body_series=VIN,**value['previous_dsm'])
            if 'get_transaction/20' in path:return {'transaction':order}
            raise AssertionError(path)
        with patch.object(dsm,'read_appointment',AsyncMock(return_value={'transaction':head('10',kind='3',status='working')})), \
                patch.object(dsm,'document_requirements',AsyncMock(return_value=value['document_policy'])), \
                patch.object(dsm,'existing_orders',AsyncMock(return_value=[])),patch.object(dsm,'call',AsyncMock(side_effect=call)), \
                patch.object(dsm,'appointment_call',AsyncMock(return_value={'status':'success','redirect_url':'/special/transaction/index/20'})) as create, \
                patch.object(dsm,'save_vehicle_details',AsyncMock()) as vehicle,patch.object(dsm,'save_client_details',AsyncMock()):
            result=await dsm.create_or_find(value,True,Mock())
            value['_created_command_id']=result['id']
            retry=await dsm.create_or_find(value,False,Mock())
        self.assertEqual(result['code'],'PHDEV-20');self.assertEqual(retry['id'],'20')
        create.assert_awaited_once();vehicle.assert_not_awaited()

    async def test_recent_dsm_data_reuses_order_without_vehicle_updates_or_duplicate_creation(self):
        value=data();policy.load_stored_vehicle(value)
        value.update(final_confirmed=True,phone_confirmed=True,extra='Nu')
        order=head('10',kind='2',status='working');order['transaction_head__description']=dsm.requirements_description(value,'Revizie')
        async def call(path,fields=None):
            if 'get_car' in path:
                return dict(car_body_series=VIN,**value['previous_dsm'])
            if 'get_transaction/10' in path:return {'transaction':order}
            raise AssertionError(path)
        with patch.object(dsm,'_search_transactions',AsyncMock(return_value=[dict(transaction_id='99',transaction_type='2',status='billed')])), \
                patch.object(dsm,'_read_details',AsyncMock(return_value={'99':{'transaction':head()}})), \
                patch.object(dsm,'call',AsyncMock(side_effect=call)),patch.object(dsm,'_create',AsyncMock()) as create, \
                patch.object(dsm,'save_vehicle_details',AsyncMock()) as vehicle,patch.object(dsm,'save_client_details',AsyncMock()):
            result=await dsm.create_or_find(value,True,Mock())
        self.assertEqual(result['code'],'PHDEV-10');self.assertEqual(result['data_source'],'dsm')
        create.assert_not_awaited();vehicle.assert_not_awaited()

    async def test_forged_recent_snapshot_is_rejected_by_fresh_dsm_history_before_creation(self):
        value=data();policy.load_stored_vehicle(value);value.update(source_type='3',final_confirmed=True,phone_confirmed=True)
        with patch.object(dsm,'read_appointment',AsyncMock(return_value={'transaction':head('10',kind='3',status='working')})), \
                patch.object(dsm,'document_requirements',AsyncMock(return_value=policy.evaluate(vehicle_exists=True,last_visit='2020-01-01',checked_on=TODAY))), \
                patch.object(dsm,'appointment_call',AsyncMock()) as create:
            with self.assertRaisesRegex(ValueError,'talon'):
                await dsm.create_or_find(value,True,Mock())
        create.assert_not_awaited()

    async def test_recent_data_does_not_bypass_the_existing_order_guard(self):
        value=data();policy.load_stored_vehicle(value)
        value.update(source_type='3',final_confirmed=True,phone_confirmed=True)
        refs=[dict(transaction_id='99',transaction_type='2',status='billed'),
              dict(transaction_id='20',transaction_type='2',status='working')]
        details={'99':{'transaction':head()},'20':{'transaction':head('20',status='working')}}
        with patch.object(dsm,'read_appointment',AsyncMock(return_value={'transaction':head('10',kind='3',status='working')})), \
                patch.object(dsm,'_search_transactions',AsyncMock(return_value=refs)), \
                patch.object(dsm,'_read_details',AsyncMock(return_value=details)), \
                patch.object(dsm,'call',AsyncMock(return_value=dict(car_body_series=VIN,**value['previous_dsm']))), \
                patch.object(dsm,'appointment_call',AsyncMock()) as create:
            with self.assertRaisesRegex(ValueError,'duplicat'):
                await dsm.create_or_find(value,True,Mock())
        create.assert_not_awaited()

    async def test_saved_vehicle_changes_are_not_overwritten_from_a_stale_snapshot(self):
        value=data();policy.load_stored_vehicle(value)
        value.update(final_confirmed=True,phone_confirmed=True)
        async def call(path,fields=None):
            if 'get_car' in path:return dict(car_body_series=VIN,car_manufacture_year='2021',car_itp_expires_date='2030-01-01')
            return {'transaction':head('10',status='working')}
        with patch.object(dsm,'document_requirements',AsyncMock(return_value=value['document_policy'])), \
                patch.object(dsm,'call',AsyncMock(side_effect=call)),patch.object(dsm,'save_vehicle_details',AsyncMock()) as update:
            with self.assertRaisesRegex(ValueError,'suprascriere'):
                await dsm.create_or_find(value,True,Mock())
        update.assert_not_awaited()

    async def test_recent_missing_itp_does_not_require_an_invented_value_to_reuse_the_order(self):
        value=data();value['previous_dsm']={};policy.load_stored_vehicle(value)
        value.update(final_confirmed=True,phone_confirmed=True,extra='Nu')
        order=head('10',status='working');order['transaction_head__description']=dsm.requirements_description(value,'Revizie')
        async def call(path,fields=None):
            return dict(car_body_series=VIN) if 'get_car' in path else {'transaction':order}
        with patch.object(dsm,'document_requirements',AsyncMock(return_value=value['document_policy'])), \
                patch.object(dsm,'call',AsyncMock(side_effect=call)),patch.object(dsm,'save_client_details',AsyncMock()), \
                patch.object(dsm,'save_vehicle_details',AsyncMock()) as update:
            result=await dsm.create_or_find(value,True,Mock())
        self.assertIsNone(result['itp']);update.assert_not_awaited()

    async def test_new_vehicle_cannot_create_an_order_using_a_generic_dsm_vehicle(self):
        value=data();value.update(vehicle_missing=True,car_id='',final_confirmed=True,phone_confirmed=True,
            source_type='3',document_verified=True,document_current=True,document_fields_confirmed=True,itp='2030-01-01')
        source=head('10',kind='3',status='working',car_id='',vin='')
        with patch.object(dsm,'read_appointment',AsyncMock(return_value={'transaction':source})), \
                patch.object(dsm,'appointment_call',AsyncMock()) as create:
            with self.assertRaisesRegex(ValueError,'înregistrată'):
                await dsm.create_or_find(value,True,Mock())
        create.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
