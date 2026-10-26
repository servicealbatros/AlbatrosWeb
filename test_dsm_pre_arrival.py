import unittest
from unittest.mock import AsyncMock,Mock,patch
import dsm_pre_arrival as prod

VIN='WV100000000000008'


def head(kind='2',status='working'):
    return dict(transaction_head__id='10',transaction_head__type=kind,transaction_head__status=status,
        transaction_head__code='PHDEV-10' if kind=='2' else 'OFERTA-10',car_body_series=VIN,
        car_auto_number='B112TST',car_id='22',contact_id='33',transaction_head__description='Revizie')


class ProductionWriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.search=AsyncMock(return_value=[])
        mock=patch.object(prod,'_search_transactions',self.search)
        mock.start();self.addCleanup(mock.stop)

    def data(self):
        return dict(production=True,vin=VIN,registration='B112TST',car_id='22',contact_id='33',
            source_transaction_id='10',source_type='2',source_code='PHDEV-10',source_explicit=True,
            final_confirmed=True,document_verified=True,document_current=True,document_fields_confirmed=True,
            phone_confirmed=True,itp='2035-01-01',manufacture_year=2020,client_name='Client',
            client_address='Ploiesti Str. Test 1',phone='40700100009',work='Revizie',extra='Nu',
            appointment_start='2030-01-01T08:30')

    async def test_existing_bound_order_is_reused_and_confirmed_itp_is_passed_exactly(self):
        data=self.data();h=head();h['transaction_head__description']=prod.requirements_description(data,'Revizie')
        receipt=Mock()
        with patch.object(prod,'call',AsyncMock(return_value={'transaction':h})), \
             patch.object(prod,'save_vehicle_details',AsyncMock()) as vehicle, \
             patch.object(prod,'save_client_details',AsyncMock()) as client,patch.object(prod,'_create',AsyncMock()) as create:
            result=await prod.create_or_find(data,True,receipt)
        create.assert_not_awaited();receipt.assert_called_once_with('10')
        self.assertEqual(result['code'],'PHDEV-10');self.assertFalse(result['test'])
        self.assertEqual(vehicle.await_args.args[0]['itp'],'2035-01-01')
        client.assert_awaited_once()

    async def test_wrong_vin_closed_order_or_missing_final_consent_cannot_write(self):
        for change in ({'car_body_series':'WBA00000000000002'},{'transaction_head__status':'completed'}):
            h=head();h.update(change)
            with patch.object(prod,'call',AsyncMock(return_value={'transaction':h})),patch.object(prod,'_create',AsyncMock()) as create:
                with self.assertRaises(ValueError):await prod.create_or_find(self.data(),True,Mock())
                create.assert_not_awaited()
        data=self.data();data['final_confirmed']=False
        with patch.object(prod,'call',AsyncMock()) as call:
            with self.assertRaises(ValueError):await prod.create_or_find(data,True,Mock())
            call.assert_not_awaited()

    async def test_uncertain_creation_is_not_repeated_and_ambiguous_open_order_requires_review(self):
        data=self.data();data.update(source_explicit=False)
        with patch.object(prod,'call',AsyncMock(return_value={'transaction':head()})),patch.object(prod,'_create',AsyncMock()) as create:
            with self.assertRaisesRegex(ValueError,'incertă'):await prod.create_or_find(data,False,Mock())
            create.assert_not_awaited()
            data['requires_order_review']=True
            with self.assertRaisesRegex(ValueError,'duplicat'):await prod.create_or_find(data,True,Mock())
            create.assert_not_awaited()

    async def test_seed_uses_exact_offer_and_verified_mobile_and_rejects_phone_mismatch(self):
        appointment=dict(registration='B112TST',operations='OFERTA 10-Revizie')
        async def call(path,data):
            if '/get_car' in path:return {'car_body_series':VIN}
            return dict(contact_name='Client',mobile=[{'checked':'checked','val':'0700100009'}],address=[])
        with patch.object(prod,'_search_transactions',AsyncMock(return_value=[dict(transaction_type='1',transaction_id=10,code='OFERTA-10',status='working')])), \
             patch.object(prod,'_read_details',AsyncMock(return_value={'10':{'transaction':head('1')}})), \
             patch.object(prod,'call',AsyncMock(side_effect=call)):
            data=await prod.seed(appointment)
            self.assertEqual(data['phone'],'40700100009');self.assertEqual(data['vin'],VIN)
            appointment['phone']='40700100010'
            with self.assertRaisesRegex(ValueError,'diferă'):await prod.seed(appointment)

    async def test_new_order_receipt_is_persisted_before_binding_and_retry_reuses_it(self):
        data=self.data();data.update(source_type='3',appointment_id=7)
        source=head('3');source['transaction_head__code']='APP-7'
        empty=dict(transaction_head__id='20',transaction_head__type='2',transaction_head__status='working',contact_id='33')
        full=head();full.update(transaction_head__id='20',transaction_head__code='PHDEV-20',
            transaction_head__description=prod.requirements_description(data,'Revizie')+'\n[ALBATROS PRE-RECEPTIE 7]')
        seen=[];loaded=False
        async def call(path,fields=None):
            nonlocal loaded
            if path.endswith('/10'):return {'transaction':source}
            if path.endswith('/20'):
                if not loaded:loaded=True;return {'transaction':empty,'transaction_rows':[]}
                return {'transaction':full}
            if path.endswith('/transaction_car'):
                self.assertEqual(seen,['20']);return {'transaction_id':'20'}
            if path.endswith('/update_transaction_details'):
                full['transaction_head__description']=fields['transaction_head__description'];return {'status':1}
            raise AssertionError(path)
        with patch.object(prod,'call',AsyncMock(side_effect=call)),patch.object(prod,'read_appointment',AsyncMock(return_value={'transaction':source})), \
             patch.object(prod,'appointment_call',AsyncMock(return_value={'status':'success','redirect_url':'/special/transaction/index/20'})) as create, \
             patch.object(prod,'save_vehicle_details',AsyncMock()),patch.object(prod,'save_client_details',AsyncMock()):
            result=await prod.create_or_find(data,True,lambda ident:seen.append(ident))
            self.assertEqual(result['id'],'20')
            data['_created_command_id']='20'
            await prod.create_or_find(data,False,Mock())
            self.assertEqual(create.await_count,1)

    async def test_native_existing_order_selection_never_forces_a_new_order(self):
        data=self.data();data['source_type']='3'
        with patch.object(prod,'read_appointment',AsyncMock(return_value={'transaction':head('3')})), \
             patch.object(prod,'appointment_call',AsyncMock(return_value={'status':'select_existing_transaction'})) as convert:
            with self.assertRaisesRegex(ValueError,'duplicat'):
                await prod.create_or_find(data,True,Mock())
            convert.assert_awaited_once_with('transformappointment',{'appointment_id':'10','target_type':'2'})

    async def test_conversion_without_receipt_or_canceled_source_never_falls_back_to_creation(self):
        data=self.data();data['source_type']='3'
        for source,response in ((head('3'),{'status':'success'}),(head('3','canceled'),{})):
            with patch.object(prod,'read_appointment',AsyncMock(return_value={'transaction':source})), \
                 patch.object(prod,'appointment_call',AsyncMock(return_value=response)), \
                 patch.object(prod,'_create',AsyncMock()) as create:
                with self.assertRaises(ValueError):await prod.create_or_find(data,True,Mock())
                create.assert_not_awaited()

    async def test_unlinked_open_work_order_prevents_native_creation_even_after_client_confirmation(self):
        self.search.return_value=[dict(transaction_id=20,transaction_type='2',status='working')]
        existing=head();existing.update(transaction_head__id='20',transaction_head__code='PHDEV-20')
        data=self.data();data['source_type']='3'
        with patch.object(prod,'_read_details',AsyncMock(return_value={'20':{'transaction':existing}})), \
             patch.object(prod,'read_appointment',AsyncMock(return_value={'transaction':head('3')})), \
             patch.object(prod,'appointment_call',AsyncMock()) as convert,patch.object(prod,'_create',AsyncMock()) as create:
            with self.assertRaisesRegex(ValueError,'duplicat'):await prod.create_or_find(data,True,Mock())
            convert.assert_not_awaited();create.assert_not_awaited()
            existing['car_body_series']='WBA00000000000002'
            with self.assertRaisesRegex(ValueError,'neconcordantă'):await prod.existing_orders(data)

    async def test_native_appointment_identifies_customer_without_history_guessing(self):
        detail=dict(carId=22,clientId=33,carNumber='B112TST',clientPhone='0700100009',
                    code='APP-10',canCancel=True,canTransform=True,start='2030-01-01T08:30')
        async def call(path,fields):
            if '/get_car' in path:return {'car_body_series':VIN}
            return dict(contact_name='Client',mobile=[dict(val='0700100009',checked='checked')],address=[[]])
        with patch.object(prod,'appointment_call',AsyncMock(return_value=dict(status='success',detail=detail))), \
             patch.object(prod,'call',AsyncMock(side_effect=call)),patch.object(prod,'_search_transactions',AsyncMock()) as search:
            data=await prod.seed(dict(registration='B112TST',dsm_transaction_id='10',dsm_transaction_type='3',operations='Revizie'))
        self.assertEqual(data['phone'],'40700100009');self.assertEqual(data['source_type'],'3')
        self.assertEqual(data['client_address'],'')
        search.assert_not_awaited()


if __name__=='__main__':unittest.main()
