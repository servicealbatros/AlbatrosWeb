import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from offer_pricing import labour_pricing, quote_pricing


class QuotePricingTests(unittest.TestCase):
    def test_net_legacy_values_and_supplier_gross_prices(self):
        draft = dict(labour_price_ron=440, parts_price_ron=300,
                     oil_unit_price_ron=50, oil_liters=6,
                     drain_plug_selected=True, drain_plug_price_ron=10)
        result = quote_pricing(draft)
        self.assertEqual(result['labour_vat_ron'], 92.4)
        self.assertEqual(result['labour_price_with_vat_ron'], 532.4)
        self.assertEqual(result['parts_total_ron'], 610)
        self.assertEqual(result['total_ron'], 1142.4)
        # Repeated saves/reads must never apply VAT twice.
        self.assertEqual(quote_pricing({**draft, **result}), result)
        self.assertEqual(quote_pricing({**draft, 'drain_plug_selected': False})['total_ron'], 1132.4)

    def test_currency_rounding_and_saved_rate(self):
        self.assertEqual(labour_pricing('0.50')['labour_vat_ron'], 0.11)
        self.assertEqual(labour_pricing('1.005')['labour_price_ron'], 1.01)
        with patch.dict('os.environ', {'ALBATROS_LABOUR_VAT_PERCENT': '0'}):
            self.assertEqual(labour_pricing(200)['labour_price_with_vat_ron'], 200)
            self.assertEqual(quote_pricing(dict(labour_price_ron=200, labour_vat_percent=21))['total_ron'], 242)

    def test_invalid_labour_is_rejected(self):
        import app
        for value in (-1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                labour_pricing(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.OfferDraftInput(labour_price_ron=value)

    def test_saved_draft_keeps_net_basis_and_reviewed_tax_rate(self):
        import app
        state = {}
        with patch.object(app, '_request_by_id', return_value={'id': 999, 'symptom': 'Rulment'}), \
             patch.object(app, '_draft_for_request', side_effect=lambda _: dict(state)), \
             patch.object(app, '_save_draft_for_request', side_effect=lambda _, d: state.update(d)):
            data = app.OfferDraftInput(labour_price_ron=440, expected_hours=2.2,
                                      technical_hours=1.8, booking_hours=3.2)
            app.save_offer_draft(999, data)
            self.assertEqual(state['labour_price_ron'], 440)
            self.assertEqual(state['labour_price_with_vat_ron'], 532.4)
            with patch.dict('os.environ', {'ALBATROS_LABOUR_VAT_PERCENT': '0'}):
                app.save_offer_draft(999, data)
            self.assertEqual(state['total_ron'], 532.4)
            self.assertFalse(state['labour_reviewed'])

    def test_empty_parts_draft_uses_new_times_without_replacing_manual_labour(self):
        import app
        draft = dict(technical_hours=0, expected_hours=0, labour_price_ron=0)
        tech = dict(status='ready', technical_hours=1.8, albatros_expected_hours=2.2,
                    labour_price_ron=440, booking_hours=3.2)
        with ExitStack() as stack:
            for name, value in {
                '_request_by_id': dict(id=999, symptom='Rulment'),
                '_load_request_offer_result': {}, '_load_offer_selections': {},
                '_load_technician_result': tech, '_draft_for_request': draft,
                '_load_robot_jobs': [],
            }.items():
                stack.enter_context(patch.object(app, name, return_value=value))
            self.assertIn('532.40 RON', app.request_unified_offer_page(999))
            draft['labour_price_ron'] = 200
            self.assertIn('242.00 RON', app.request_unified_offer_page(999))


class QuoteDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_send_routes_keep_approval_and_share_reviewed_quote(self):
        import app
        from fastapi import HTTPException
        with patch.object(app, '_confirm'), patch.object(app, '_confirm_local_dashboard_click'), \
             patch.object(app, '_request_by_id', return_value={'id': 999}), \
             patch.object(app, '_require_advisor_labour_review'), \
             patch.object(app, '_draft_for_request', return_value={}), \
             patch.object(app, '_load_request_offer_result', return_value={'review_status': 'APPROVED'}), \
             patch.object(app, '_send_reviewed_offer_draft', AsyncMock(return_value={
                 'labour_price_ron': 200, 'parts_total_ron': 100, 'total_ron': 342})) as send:
            self.assertEqual((await app.request_full_offer_send_whatsapp(999))['total_ron'], 342)
            self.assertEqual((await app.request_offer_send_whatsapp(999, MagicMock()))['total_ron'], 342)
            self.assertEqual(send.await_count, 2)
            with patch.object(app, '_load_request_offer_result', return_value={'review_status': 'READY_FOR_REVIEW'}):
                with self.assertRaises(HTTPException):
                    await app.request_full_offer_send_whatsapp(999)
                with self.assertRaises(HTTPException):
                    await app.request_offer_send_whatsapp(999, MagicMock())
            self.assertEqual(send.await_count, 2)

    async def test_customer_message_template_and_remembered_context_use_same_gross(self):
        import app
        for long_description in (False, True):
            with self.subTest(shortened_template=long_description), ExitStack() as stack:
                draft = dict(labour_price_ron=440, hourly_rate_ron=200, expected_hours=2.2,
                             booking_hours=3.2, parts_price_ron=100, operation='Rulment',
                             parts_name=('Piesă verificată ' * 120 if long_description else 'Rulment'),
                             additional_hours=0.2, additional_operation='Măsurare')
                mocks = {
                    '_confirm_local_dashboard_click': None,
                    '_request_by_id': dict(id=999, symptom='Rulment', phone='0700000000'),
                    '_draft_for_request': draft, '_require_advisor_labour_review': None,
                    '_load_offer_selections': {}, '_remember_last_sent_offer': None,
                    '_set_offer_flow_state': None, 'save_request_status': None,
                    '_reset_beta_intake_after_offer': None,
                }
                for name, value in mocks.items():
                    stack.enter_context(patch.object(app, name, return_value=value))
                stack.enter_context(patch.object(app.sqlite3, 'connect', return_value=MagicMock()))
                stack.enter_context(patch.object(app, 'REQUEST_STATUS', {}))
                send = stack.enter_context(patch.object(app, 'send_whatsapp_notification', AsyncMock()))
                result = await app.send_offer_draft_whatsapp(999, MagicMock())
                message = send.call_args.args[1]
                template = send.call_args.kwargs['template_text']
                for text in (message, template):
                    self.assertIn('cu TVA', text)
                    self.assertIn('532,40 RON', text)
                    self.assertIn('632,40 RON', text)
                    self.assertIn('48,40 RON', text)
                self.assertEqual(result['total_ron'], 632.4)
                context = app._remember_last_sent_offer.call_args.args[3]
                self.assertEqual(context['labour_price_ron'], 440)
                self.assertEqual(context['labour_price_with_vat_ron'], 532.4)


class QuoteBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_offer_editor_dock_follows_initial_and_edited_gross_amounts(self):
        import app
        from playwright.async_api import async_playwright
        draft = dict(labour_price_ron=440, technical_hours=1.8, expected_hours=2.2,
                     booking_hours=3.2, parts_price_ron=100)
        with ExitStack() as stack:
            for name, value in {
                '_request_by_id': dict(id=999, symptom='Rulment'),
                '_load_request_offer_result': {}, '_load_offer_selections': {},
                '_load_technician_result': {}, '_draft_for_request': draft,
                '_load_robot_jobs': [],
            }.items():
                stack.enter_context(patch.object(app, name, return_value=value))
            html = app.request_unified_offer_page(999)
        if not isinstance(html, str):
            html = html.body.decode()
        async with async_playwright() as driver:
            if not Path(driver.chromium.executable_path).exists():
                self.skipTest('Pinned Playwright Chromium is not installed')
            browser = await driver.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                page = await browser.new_page()
                await page.route('**/*', lambda route: route.abort())
                await page.set_content(html)
                await page.wait_for_function("document.getElementById('dockLabour').textContent.includes('532,40')")
                self.assertIn('632,40', await page.locator('#dockTotal').inner_text())
                self.assertIn('100,00', await page.locator('#dockParts').inner_text())
                await page.locator('#labour_price_ron').fill('200')
                await page.wait_for_function("document.getElementById('dockLabour').textContent.includes('242,00')")
                self.assertIn('342,00', await page.locator('#dockTotal').inner_text())
                await page.locator('#hourly_rate_ron').fill('300')
                await page.wait_for_function("document.getElementById('dockLabour').textContent === document.getElementById('labour_gross_value').textContent")
                net = float(await page.locator('#labour_price_ron').input_value())
                expected = labour_pricing(net)['labour_price_with_vat_ron']
                self.assertIn(f'{expected:.2f}'.replace('.', ','), await page.locator('#dockLabour').inner_text())
                await page.locator('#labour_price_ron').fill('0.50')
                await page.wait_for_function("document.getElementById('dockLabour').textContent.includes('0,61')")
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
