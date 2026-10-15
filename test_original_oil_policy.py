import unittest
from unittest.mock import patch

from original_oil_policy import is_original_engine_oil
from autototal_request_worker import _choose_engine_oil_card


class OriginalOilTests(unittest.TestCase):
    def test_approval_does_not_turn_aftermarket_into_original(self):
        self.assertFalse(is_original_engine_oil({'title': 'CASTROL EDGE BMW LL04', 'brand': 'CASTROL'}, 'BMW 320D'))
        self.assertFalse(is_original_engine_oil({'title': 'MOTUL BMW LL04', 'brand': 'BMW'}, 'BMW 320D'))
        self.assertFalse(is_original_engine_oil({'title': 'BMW 0W30'}, 'FORD FOCUS'))
        self.assertFalse(is_original_engine_oil({'title': 'BMW 0W30'}, ''))
        self.assertTrue(is_original_engine_oil({'title': 'BMW 8321 OIL | ULEI MOTOR'}, 'BMW 320D'))
        self.assertTrue(is_original_engine_oil({'title': 'VW OIL'}, 'SKODA OCTAVIA'))

    def test_oil_must_have_original_brand_approval_viscosity_and_quantity(self):
        requirements = {'oe_brand': 'BMW', 'engine_capacity': {'value': 5.5},
                        'engine_oil_options': [{'classifications': ['BMW LL12-FE'], 'grades': ['0W-30']}]}
        card = {'title': 'BMW OIL EXAMPLE | ULEI MOTOR',
                'text': 'BMW LL12-FE 0W30 Ambalare: 1L Pret vanzare (cu TVA) 100.00'}
        oil = _choose_engine_oil_card([card], requirements)
        self.assertEqual(oil['quantity'], 6)
        self.assertEqual(oil['brand'], 'BMW')
        self.assertIsNone(_choose_engine_oil_card([{**card, 'title': 'MOTUL SPECIFIC BMW LL12-FE'}], requirements))
        self.assertIsNone(_choose_engine_oil_card([{**card, 'text': card['text'].replace('LL12-FE', 'LL04')}], requirements))
        self.assertIsNone(_choose_engine_oil_card([card], {**requirements, 'oe_brand': ''}))
        self.assertIsNone(_choose_engine_oil_card([card], {**requirements, 'engine_capacity': {'value': 0}}))

    def test_stale_aftermarket_selection_blocks_approval_or_sending(self):
        import app
        with patch.object(app, '_request_by_id', return_value={'vehicle': 'BMW 320D'}), patch.object(app, '_load_offer_selections', return_value={'40': {'products': [{'requested_item': 'ulei motor', 'description': 'MOTUL BMW LL04', 'brand': 'MOTUL'}]}}):
            with self.assertRaises(app.HTTPException):
                app._require_original_oil_selection(40)


if __name__ == '__main__':
    unittest.main()
