import unittest
from unittest.mock import AsyncMock, patch

from customer_offer_text import customer_offer_text, customer_oil_specification


class CustomerOfferTextTests(unittest.TestCase):
    def test_photo_oil_specification_attributes_requirements_to_automaker(self):
        source = ('BMW Longlife-12 FE 0W-30 / 5W-30 conform opțiunilor AutoTotal '
                  'pentru VIN; produsul final de verificat')
        public = customer_oil_specification(source)
        self.assertNotIn('AutoTotal', public)
        self.assertIn('cerințelor producătorului auto', public)
        self.assertIn('BMW Longlife-12 FE 0W-30 / 5W-30', public)
        # Cleaning the source attribution must not turn alternatives or an
        # unconfirmed product into an automatically confirmed selection.
        self.assertIn('produsul final de verificat', public)

    def test_removes_sources_and_links_but_keeps_brands_prices_and_notes(self):
        source = ('Piese BMW, MANN-FILTER, TRW: 5.486,53 RON\n'
                  'Sursa: AutoTotal și Inter Cars\n'
                  'Manoperă: 4.280,00 RON; 21.4 h din HaynesPro\n'
                  'BMW LL-04; MB 229.51; VW 507.00; 6 L × 121,00 RON/L\n'
                  'https://www3.eoriginal.ro/search/BMW\n'
                  'TecRMI / DSM / Q Service Manager / eOriginal\n'
                  'Total: 10.517,19 RON')
        public = customer_offer_text(source)
        for private in ('AutoTotal', 'Inter Cars', 'HaynesPro', 'TecRMI', 'DSM', 'Q Service Manager', 'eOriginal', 'eoriginal.ro'):
            self.assertNotIn(private, public)
        for retained in ('BMW, MANN-FILTER, TRW', '5.486,53 RON', '4.280,00 RON',
                         '21.4 h', 'BMW LL-04', 'MB 229.51', 'VW 507.00', '6 L', '10.517,19 RON'):
            self.assertIn(retained, public)
        self.assertEqual(customer_offer_text(public), public)


class CustomerOfferDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_free_form_offer_is_cleaned_before_delivery(self):
        import app
        with patch.object(app, '_has_open_whatsapp_window', return_value=True), \
             patch.object(app, 'send_whatsapp_text', AsyncMock(return_value={'messages': []})) as send:
            await app.send_whatsapp_notification('0700100003', 'Piese BMW din AutoTotal: 100 RON', kind='offer')
        self.assertEqual(send.call_args.args[1], 'Piese BMW: 100 RON')

    async def test_template_offer_is_cleaned_independently_of_long_message(self):
        import app
        with patch.object(app, '_has_open_whatsapp_window', return_value=False), \
             patch.dict(app.os.environ, {'WHATSAPP_TEMPLATE_OFFER': 'test-offer'}), \
             patch.object(app, 'send_whatsapp_template', AsyncMock(return_value={'messages': []})) as send:
            await app.send_whatsapp_notification('0700100003', 'Ofertă AutoTotal', kind='offer',
                template_text='BMW LL-04 conform opțiunilor AutoTotal pentru VIN; 5W-30; 6 L; 100 RON')
        public = send.call_args.args[2][2]
        self.assertNotIn('AutoTotal', public)
        self.assertIn('producătorului auto', public)
        self.assertIn('5W-30; 6 L; 100 RON', public)

    async def test_internal_mechanic_message_is_not_rewritten(self):
        import app
        with patch.object(app, '_has_open_whatsapp_window', return_value=True), \
             patch.object(app, 'send_whatsapp_text', AsyncMock(return_value={'messages': []})) as send:
            await app.send_whatsapp_notification('0700100003', 'Verifică instrucțiunea în TecRMI', kind='mechanic_schedule')
        self.assertEqual(send.call_args.args[1], 'Verifică instrucțiunea în TecRMI')


if __name__ == '__main__':
    unittest.main()
