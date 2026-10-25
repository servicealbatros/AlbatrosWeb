import copy
import csv
import io
import tempfile
from pathlib import Path
import unittest
from dsm_brake_packages import component_categories,replacement_labor,ingest,search,report_csv


class BrakePackagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'brakes.sqlite3'
        self.doc={'transaction_id':'20','document_type':'2','status':'billed','document_code':'PHDEV-20','registration':'B100 TST','vin':'WBA00000000000006','vehicle':'BMW','service_date':'2026-10-09','description':'Frane','parts':[{'code':'PAD','manufacturer':'ATE','name':'Set placute frana, frana disc','quantity':'1','unit':'set'}],'labors':[{'name':'Inlocuit placute frana fata','quantity':'1'}],'invoice_info':{'invoice_head__id':40,'transaction_head__type_2':'F'},'invoices':[{'transaction_id':'40','status':'3','code':'F40','transaction__datetime':'2026-10-09'}]}
    def tearDown(self):self.tmp.cleanup()

    def test_pads_not_discs_and_shoes_not_drums(self):
        self.assertEqual(component_categories('Set placute frana, frana disc'),{'pads'})
        self.assertEqual(component_categories('Set garnituri frana, frana tambur'),{'shoes'})

    def test_accessories_clutch_and_number_plate_are_not_replacements(self):
        for text in ['Inlocuit senzor uzura placute frana','Inlocuit set arculete saboti frana','Inlocuit taler disc frana','Inlocuit kit ambreiaj (placa,disc,rulment)','Inlocuit lampi placuta inmatriculare','Inlocuire punte spate Frana cu tambur spate','Inlocuire butuc de roata (frane cu disc)']:
            self.assertEqual(replacement_labor({'name':text,'quantity':1})[0],set(),text)
        for text in ['Set montaj saboti frana','Set parghii reglaj saboti frana','Set fixare placute frana']:
            self.assertFalse(component_categories(text),text)
            self.assertFalse(replacement_labor({'name':'Inlocuit '+text,'quantity':1})[0],text)

    def test_inspection_adjustment_and_reinstallation_are_review(self):
        for text in ['Verificat saboti frana spate','Reglaj saboti','Demontat/remontat discuri frana fata','Curatare discuri de frana','Verificare fara inlocuire placute frana']:
            actual,mentioned=replacement_labor({'name':text,'quantity':1});self.assertFalse(actual,text);self.assertTrue(mentioned,text)

    def test_all_discs_plural_and_compound_labor(self):
        self.assertEqual(replacement_labor({'name':'Inlocuirea tuturor discurilor de frana','quantity':1})[0],{'discs'})
        self.assertEqual(replacement_labor({'name':'Inlocuit discuri si placute frana fata','quantity':1})[0],{'pads','discs'})

    def test_invoice_unique_across_jobs_and_categories(self):
        d=copy.deepcopy(self.doc);d['transaction_id']='21';d['parts'][0].update(code='DISC',name='Disc frana',quantity='2',unit='pc');d['labors'][0]['name']='Inlocuit discuri frana fata'
        s=ingest([self.doc,d],self.db)
        self.assertEqual(s['confirmed_invoices'],1);self.assertEqual(s['categories']['pads']['confirmed_invoices'],1);self.assertEqual(s['categories']['discs']['confirmed_invoices'],1)

    def test_labor_only_is_counted_but_cannot_export_parts(self):
        d=copy.deepcopy(self.doc);d['parts']=[]
        s=ingest([d],self.db);self.assertEqual(s['categories']['pads']['labor_only'],1)
        c=search('B100TST',path=self.db)['jobs'][0]['components'][0]
        self.assertFalse(c['ready']);self.assertEqual(c['status'],'labor_only')

    def test_parts_with_no_replacement_are_review(self):
        d=copy.deepcopy(self.doc);d['labors'][0]['name']='Verificat placute frana fata'
        s=ingest([d],self.db);self.assertEqual(s['confirmed_invoices'],0);self.assertEqual(s['review_invoices'],1)
        self.assertEqual(search(path=self.db)['total'],0)
        self.assertEqual(search(confirmed_only=False,path=self.db)['total'],1)

    def test_receipt_or_cancelled_invoice_does_not_count(self):
        for mode in ['receipt','cancelled']:
            d=copy.deepcopy(self.doc)
            if mode=='receipt':d['invoice_info']['transaction_head__type_2']='B'
            else:d['invoices'][0]['status']='4'
            self.assertEqual(ingest([d],self.db)['confirmed_invoices'],0)

    def test_quantity_and_unit_retained_and_returns_block_copy(self):
        ingest([self.doc],self.db);c=search('B100TST',path=self.db)['jobs'][0]['components'][0]
        self.assertTrue(c['ready']);self.assertEqual((c['parts'][0]['quantity'],c['parts'][0]['unit']),(1.0,'set'))
        d=copy.deepcopy(self.doc);d['parts'].append({**d['parts'][0],'quantity':'-1'})
        ingest([d],self.db);self.assertFalse(search('B100TST',path=self.db)['jobs'][0]['components'][0]['ready'])

    def test_csv_category_filter_and_vin_conflict(self):
        d=copy.deepcopy(self.doc);d['transaction_id']='21';d['vin']='WBA00000000000002'
        ingest([self.doc,d],self.db)
        self.assertTrue(search('B100TST',path=self.db)['ambiguous_registration'])
        rows=list(csv.reader(io.StringIO(report_csv('pads',path=self.db).lstrip('\ufeff')),delimiter=';'))
        self.assertEqual(len(rows),3);self.assertIn('PAD [ATE]',rows[1])
        self.assertEqual(len(list(csv.reader(io.StringIO(report_csv('drums',path=self.db).lstrip('\ufeff')),delimiter=';'))),1)
        self.assertEqual(len(list(csv.reader(io.StringIO(report_csv('pads',path=self.db,vin=self.doc['vin']).lstrip('\ufeff')),delimiter=';'))),2)
        self.assertEqual(len(list(csv.reader(io.StringIO(report_csv(path=self.db,review_only=True).lstrip('\ufeff')),delimiter=';'))),1)

if __name__=='__main__':unittest.main()
