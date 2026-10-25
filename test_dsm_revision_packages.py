import copy
import json
from pathlib import Path
import tempfile
import unittest

from dsm_revision_packages import ingest, search, package_parts, invoice_evidence, category


class RevisionPackagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'packages.sqlite3'
        self.record={'transaction_id':'20','document_type':'2','status':'billed','document_code':'PHDEV-20','registration':'B100 TST','vin':'WBA00000000000006','vehicle':'BMW','service_date':'2026-10-09','description':'Revizie','parts':[{'code':'HU6014Z','manufacturer':'MANN','name':'Filtru ulei','quantity':'1','unit':'pc'},{'code':'OIL5W30','manufacturer':'BMW','name':'Ulei motor 5W30','quantity':'5.5','unit':'l'}],'labors':[{'name':'Schimb ulei si filtru','quantity':1}],'parent_id':'10','parent_type':'1','invoice_info':{'invoice_head__id':40,'transaction_head__type_2':'F'},'invoices':[{'transaction_id':'40','status':'3','code':'F40','transaction__datetime':'2026-10-09'}]}
        self.offer={'transaction_id':'10','document_type':'1','status':'completed','description':'Revizie','parts':[],'labors':[],'children':[{'transaction_id':'20'}]}

    def tearDown(self):self.tmp.cleanup()

    def test_linked_offer_counted_once_and_exportable(self):
        stats=ingest([self.offer,self.record],self.db)
        self.assertEqual(stats['offers_with_invoice'],1)
        p=search('ph-01 abc',path=self.db)['packages'][0]
        self.assertTrue(p['ready']);self.assertEqual(p['offer_ids'],['10'])
        self.assertEqual(p['parts'][1]['quantity'],5.5)

    def test_billed_status_does_not_replace_invoice(self):
        d=copy.deepcopy(self.record);d['invoices']=[]
        ingest([self.offer,d],self.db)
        self.assertFalse(search('B100TST',path=self.db)['packages'][0]['ready'])
        self.assertEqual(invoice_evidence(d),[])

    def test_canceled_invoice_and_receipt_excluded(self):
        for field,value in [('status','4')]:
            d=copy.deepcopy(self.record);d['invoices'][0][field]=value
            self.assertEqual(invoice_evidence(d),[])
        d=copy.deepcopy(self.record);d['invoice_info']['transaction_head__type_2']='B'
        self.assertEqual(invoice_evidence(d),[])

    def test_decimal_units_and_brands_not_mixed(self):
        rows=[{'code':'A','name':'Ulei motor','manufacturer':'M','quantity':q,'unit':'l'} for q in ['0.1','0.2']]
        rows.append({**rows[0],'unit':'pc','quantity':'1'})
        rows.append({**rows[0],'manufacturer':'N','quantity':'2'})
        r=package_parts(rows)
        self.assertEqual(len(r),3);self.assertEqual(r[0]['quantity'],0.3)

    def test_plate_reuse_requires_vin_even_without_second_revision(self):
        other={'transaction_id':'30','document_type':'2','status':'billed','registration':'B100TST','vin':'WBA00000000000002','parts':[],'labors':[]}
        ingest([self.record,other],self.db)
        self.assertTrue(search('B100TST',path=self.db)['ambiguous_registration'])
        self.assertFalse(search('B100TST',self.record['vin'],self.db)['ambiguous_registration'])

    def test_negative_rows_and_fractional_packages_block_ready(self):
        d=copy.deepcopy(self.record);d['parts'][1]['unit']='pc'
        ingest([d],self.db);self.assertFalse(search('B100TST',path=self.db)['packages'][0]['ready'])
        d=copy.deepcopy(self.record);d['parts'].append({**d['parts'][0],'quantity':'-1'})
        ingest([d],self.db);self.assertFalse(search('B100TST',path=self.db)['packages'][0]['ready'])

    def test_transmission_oil_is_not_engine_oil(self):
        self.assertEqual(category({'name':'Ulei cutie automata 5W30'}),'other')
        self.assertEqual(category({'name':'Filtru ulei cutie viteze'}),'other')

    def test_offer_appointment_order_chain(self):
        d=copy.deepcopy(self.record);d['parent_id']='15';d['parent_type']='3'
        offer=copy.deepcopy(self.offer);offer['children']=[{'transaction_id':'15'}]
        appointment={'transaction_id':'15','document_type':'3','parent_id':'10','parent_type':'1','parts':[],'labors':[]}
        stats=ingest([offer,appointment,d],self.db)
        self.assertEqual(stats['offers_with_invoice'],1)
        self.assertEqual(search('B100TST',path=self.db)['packages'][0]['offer_ids'],['10'])

    def test_unrelated_offer_for_same_plate_is_not_invoice_evidence(self):
        d=copy.deepcopy(self.record);d['parent_id']=None;d['parent_type']=None
        offer=copy.deepcopy(self.offer);offer['registration']=d['registration'];offer['children']=[]
        stats=ingest([offer,d],self.db)
        self.assertEqual(stats['invoiced_revision_orders'],1)
        self.assertEqual(stats['offers_with_invoice'],0)

    def test_revision_consumables_included_other_repairs_separate(self):
        d=copy.deepcopy(self.record)
        d['parts'] += [{'code':'PLUG','manufacturer':'ELRING','name':'Buson baie ulei','quantity':'1','unit':'pc'},{'code':'HEAD','manufacturer':'ELRING','name':'Garnitura capac chiulasa','quantity':'1','unit':'pc'}]
        ingest([d],self.db);p=search('B100TST',path=self.db)['packages'][0]
        self.assertIn('PLUG',[r['code'] for r in p['parts']])
        self.assertEqual([r['code'] for r in p['other_parts']],['HEAD'])

    def test_zero_labor_quantity_not_ready(self):
        d=copy.deepcopy(self.record);d['labors'][0]['quantity']='0'
        ingest([d],self.db);self.assertFalse(search('B100TST',path=self.db)['packages'][0]['ready'])

    def test_appointment_explicit_offer_reference(self):
        d=copy.deepcopy(self.record);d['parent_id']='15';d['parent_type']='3'
        offer=copy.deepcopy(self.offer);offer['children']=[]
        appointment={'transaction_id':'15','document_type':'3','source_offer_id':10,'parts':[],'labors':[]}
        self.assertEqual(ingest([offer,appointment,d],self.db)['offers_with_invoice'],1)

    def test_both_ends_prove_appointment_link_without_fetching_appointment(self):
        d=copy.deepcopy(self.record);d['parent_id']='15';d['parent_type']='3'
        offer=copy.deepcopy(self.offer);offer['children']=[{'transaction_id':'15','transaction_head__type':'3'}]
        self.assertEqual(ingest([offer,d],self.db)['offers_with_invoice'],1)


if __name__=='__main__':unittest.main()
