import unittest
from unittest.mock import patch

from offer_parts_checklist import is_workshop_consumable, material_key, split_workshop_materials, subtotal, render_internal_materials


class WorkshopConsumablesTests(unittest.TestCase):
    def setUp(self):
        self.paste = dict(description='Pastă montaj injectoare', code='83238854223', quantity=None,
                          unit_price_ron=None, confirmed=False)
        self.gasket = dict(description='Garnitură capac chiulasă', code='11128473265', quantity=1,
                           unit_price_ron=219.04, confirmed=True)

    def test_scope_keeps_repair_parts_and_service_fluids_billable(self):
        for name in ['Pastă de etanșare', 'Cârpe de șters', 'Lavete', 'Degresant', 'Spray curățare']:
            self.assertTrue(is_workshop_consumable({'description': name}), name)
        for name in ['Garnitură capac chiulasă', 'Șaibe de foc', 'Simering palier', 'Ulei motor original',
                     'Filtru combustibil', 'Antigel BMW LC18', 'Șuruburi și etanșări']:
            self.assertFalse(is_workshop_consumable({'description': name}), name)

    def test_internal_materials_never_enter_initial_total_even_with_forged_request(self):
        forged = dict(self.paste, stock_request=dict(technician='test', requested_at='test', quantity=1))
        billed, internal = split_workshop_materials([self.gasket, forged], [self.paste])
        self.assertEqual(subtotal(billed), 219.04)
        self.assertEqual(len(internal), 1)
        self.assertNotIn('stock_request', internal[0])
        html = render_internal_materials(internal)
        self.assertIn('Nu sunt incluse în oferta inițială', html)
        self.assertNotIn('quote-part-row', html)

    def test_recorded_request_only_then_enters_draft_and_survives_legacy_save(self):
        import app
        state = dict(parts_checklist=[self.gasket, self.paste])
        def save(_id, draft):
            state.clear()
            state.update(draft)
        with patch.object(app, '_request_by_id', return_value={'id': 999, 'symptom': 'Garnitură capac'}), \
             patch.object(app, '_draft_for_request', side_effect=lambda _: dict(state)), \
             patch.object(app, '_save_draft_for_request', side_effect=save):
            app.save_offer_draft(999, app.OfferDraftInput(**state))
            self.assertEqual(state['parts_price_ron'], 219.04)
            self.assertFalse(state['parts_prices_pending'])
            self.assertEqual(len(state['internal_consumables']), 1)
            app.record_workshop_stock_request(999, app.WorkshopStockRequestInput(
                item_key=material_key(self.paste), technician='Mecanic test', quantity=0.1,
                unit_price_ron=100, reference='BON-TEST'))
            self.assertEqual(state['parts_price_ron'], 229.04)
            self.assertFalse(state['labour_reviewed'])
            self.assertEqual(state['internal_consumables'], [])
            self.assertEqual(state['parts_checklist'][-1]['stock_request']['reference'], 'BON-TEST')
            # The existing editor does not send audit metadata. The server retains it.
            legacy = [dict(row) for row in state['parts_checklist']]
            legacy[-1].pop('stock_request')
            app.save_offer_draft(999, app.OfferDraftInput(parts_checklist=legacy))
            self.assertEqual(state['parts_price_ron'], 229.04)
            self.assertIn('stock_request', state['parts_checklist'][-1])
            from fastapi import HTTPException
            with self.assertRaises(HTTPException):
                app.record_workshop_stock_request(999, app.WorkshopStockRequestInput(
                    item_key=material_key(self.paste), technician='Mecanic test', quantity=0.1, unit_price_ron=100))
            legacy[-1]['quantity'] = 2
            with self.assertRaises(HTTPException):
                app.save_offer_draft(999, app.OfferDraftInput(parts_checklist=legacy))


if __name__ == '__main__':
    unittest.main()
