import json
import unittest
from unittest.mock import AsyncMock, patch

from autototal_request_worker import (
    _autototal_oe_card_identity, _keep_one_oil_drain_plug, _search_oe_code_options_once,
)


class OEIdentityTests(unittest.TestCase):
    def test_observed_retailer_heading_keeps_exact_bmw_reference(self):
        row = _autototal_oe_card_identity('AUTOTOTAL OE 11318570649 (AUTOTOTAL) | 11318570649 LANT DISTRIBUTIE SUPERIOR - O.E. BMW')
        self.assertEqual(row, {'brand': 'OE BMW', 'manufacturer': 'BMW', 'manufacturer_ref': '11318570649'})

    def test_header_formats_and_other_makers(self):
        for title in ['AUTOTOTAL OE 11 31 8 570 649 | LANT - O.E. BMW', 'OE BMW 11318570649 | LANT']:
            self.assertEqual(_autototal_oe_card_identity(title)['manufacturer_ref'], '11318570649')
        self.assertEqual(_autototal_oe_card_identity('AUTOTOTAL OE 04E115561H | FILTRU - O.E. VW')['brand'], 'OE VW')

    def test_equivalence_or_approval_does_not_make_a_product_original(self):
        for title in ['FEBI 123 | ECHIVALENT O.E. BMW', 'CASTROL OIL | BMW LL04',
                      'AUTOTOTAL 123 | ECHIVALENT O.E. BMW', 'AUTOTOTAL OE 123 | FARA PRODUCATOR',
                      'AUTOTOTAL OE 123 | O.E. BMW-B']:
            with self.subTest(title=title):
                self.assertIsNone(_autototal_oe_card_identity(title))

    def test_exact_original_plug_wins_over_cheaper_equivalent(self):
        original = {'requested_item': 'bușon baie ulei', 'brand': 'OE BMW',
                    'manufacturer_ref': '11137535106', 'oe_code': '11137535106', 'price_ron': 24.66}
        equivalent = {**original, 'brand': 'ELRING', 'manufacturer_ref': '427.210', 'price_ron': 12.29}
        self.assertEqual(_keep_one_oil_drain_plug([equivalent, original]), [original])


class OEPriceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def search(self, code):
        cards = [dict(title='AUTOTOTAL OE 11318570649 (AUTOTOTAL) | LANT - O.E. BMW',
                      text='Pret vanzare (cu TVA)\n382.08\nComanda ajunge maine - 08:00 din Ploiesti',
                      url='https://www3.eoriginal.ro/search/example')]
        with patch('autototal_request_worker._evaluate', AsyncMock(side_effect=[True, cards[0]['url']])), \
             patch('autototal_request_worker._wait_value', AsyncMock(side_effect=[True, json.dumps(cards)])), \
             patch('autototal_request_worker.asyncio.sleep', AsyncMock()):
            return await _search_oe_code_options_once(None, {'oe_code': code, 'requested_item': 'Lanț distribuție superior'}, 1000)

    async def test_observed_oe_card_gets_price_and_clean_reference(self):
        rows = await self.search('11318570649')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['brand'], 'OE BMW')
        self.assertEqual(rows[0]['manufacturer_ref'], '11318570649')
        self.assertEqual(rows[0]['price_ron'], 382.08)

    async def test_different_oe_number_does_not_inherit_this_price(self):
        self.assertEqual(await self.search('13528490224'), [])


if __name__ == '__main__':
    unittest.main()
