import unittest
from copy import deepcopy
from unittest.mock import patch
from fastapi import HTTPException
import app

class ManualOfferTests(unittest.TestCase):
 def setUp(self):
  self.selections={}; self.drafts={'42':{'labour_reviewed':True,'parts_checklist':[dict(description='Rulment roata',code='',quantity=1,unit_price_ron=None,confirmed=False)]}}
  self.patches=[patch.object(app,'_request_by_id',return_value={'id':42,'symptom':'Rulment roata','vin':'TEST'}),patch.object(app,'_load_offer_selections',side_effect=lambda:deepcopy(self.selections)),patch.object(app,'_save_offer_selections',side_effect=lambda v:self.selections.update(deepcopy(v))),patch.object(app,'_load_offer_drafts',side_effect=lambda:deepcopy(self.drafts)),patch.object(app,'_save_offer_drafts',side_effect=lambda v:self.drafts.update(deepcopy(v)))]
  for p in self.patches:p.start();self.addCleanup(p.stop)
 def test_price_quantity_and_confirmed_line_survive_reload_and_removal(self):
  result=app.request_offer_manual_part(42,app.ManualOfferPartInput(description='Rulment roata',manufacturer_ref='TEST-123',quantity=2,unit_price_ron=125.50))
  draft=app._draft_for_request(42)
  self.assertEqual(draft['parts_price_ron'],251)
  self.assertFalse(draft['labour_reviewed'])
  self.assertEqual(len(draft['parts_checklist']),1)
  line=draft['parts_checklist'][0];self.assertEqual(line['code'],'TEST-123');self.assertTrue(line['confirmed'])
  self.assertEqual(result['selected'][0]['price_ron'],125.50)
  app.request_offer_manual_part_remove(42,app.ManualOfferPartRemoveInput(product_code=result['product']['supplier_code']))
  self.assertEqual(app._draft_for_request(42)['parts_price_ron'],0)
  self.assertEqual(self.selections['42']['products'],[])
 def test_invalid_prices_do_not_change_quote(self):
  for price in (-1,float('nan'),float('inf'),1000001):
   with self.subTest(price=price),self.assertRaises(HTTPException):
    app.request_offer_manual_part(42,app.ManualOfferPartInput(description='Rulment roata',manufacturer_ref='TEST',unit_price_ron=price))
  self.assertEqual(self.selections,{})
 def test_additional_part_keeps_existing_quote_lines(self):
  app.request_offer_manual_part(42,app.ManualOfferPartInput(description='Butuc',manufacturer_ref='TEST-456',quantity=1,unit_price_ron=200))
  self.assertEqual(len(self.drafts['42']['parts_checklist']),2)
  self.assertFalse(self.drafts['42']['parts_checklist'][0]['confirmed'])
  self.assertEqual(self.drafts['42']['parts_price_ron'],200)

if __name__=='__main__':unittest.main()
