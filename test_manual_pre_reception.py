import unittest
from unittest.mock import AsyncMock, Mock, patch
import manual_pre_reception as m


class ManualTestSafety(unittest.IsolatedAsyncioTestCase):
    def data(self):
        return dict(manual_test=True,test_run_id='owner-test',registration='B199TST',
            vin='VF100000000000001',car_id='22',contact_id='33',source_transaction_id='10',
            document_verified=True,document_current=True,document_fields_confirmed=True,final_confirmed=True,
            itp='2027-10-30',manufacture_year=2016,client_name='Test Client',
            client_address='Test address 10',phone='40700100000',work='Test',extra='Nu')

    def head(self,ident):
        d=self.data()
        return dict(transaction_head__id=ident,transaction_head__type='2',transaction_head__status='working',
            car_body_series=d['vin'],car_id=d['car_id'],contact_id=d['contact_id'],
            transaction_head__description='Test',transaction_head__code='PHDEV-TEST')

    async def test_existing_order_is_reused_without_creation(self):
        d=self.data();head=self.head('55');desired=m.requirements_description(d,'Test')+'\n[TEST PRE-RECEPTIE B199TST]'
        async def call(path,payload=None):
            if 'start_transaction' in path:raise AssertionError('duplicate order')
            if 'update_transaction_details' in path:return {'status':1}
            if path.endswith('/55') and call.written:return {'transaction':{**head,'transaction_head__description':desired}}
            if path.endswith('/55'):
                call.written=True;return {'transaction':head}
            return {'transaction':self.head('10')}
        call.written=False
        record=Mock()
        with patch.object(m,'call',side_effect=call),patch.object(m,'existing_orders',AsyncMock(return_value=[{'id':'55','code':'PHDEV-TEST'}])), \
             patch.object(m,'save_vehicle_details',AsyncMock()),patch.object(m,'save_client_details',AsyncMock()):
            result=await m.write(d,True,record)
        self.assertEqual(result['id'],'55');record.assert_called_once_with('55')

    async def test_confirmation_is_required_before_any_dsm_access(self):
        d=self.data();d['document_fields_confirmed']=False
        with patch.object(m,'call',AsyncMock()) as call:
            with self.assertRaises(ValueError):await m.write(d,True,Mock())
            call.assert_not_called()

    async def test_uncertain_previous_creation_is_not_repeated(self):
        d=self.data()
        with patch.object(m,'call',AsyncMock(return_value={'transaction':self.head('10')})) as call, \
             patch.object(m,'existing_orders',AsyncMock(return_value=[])):
            with self.assertRaises(ValueError):await m.write(d,False,Mock())
            self.assertEqual(call.await_count,1)

    async def test_multiple_existing_orders_block_creation(self):
        with patch.object(m,'call',AsyncMock(return_value={'transaction':self.head('10')})) as call, \
             patch.object(m,'existing_orders',AsyncMock(return_value=[{'id':'55'},{'id':'56'}])):
            with self.assertRaises(ValueError):await m.write(self.data(),True,Mock())
            self.assertEqual(call.await_count,1)
