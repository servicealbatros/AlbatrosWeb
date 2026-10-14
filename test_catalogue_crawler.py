import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from catalogue_crawler import safe_url, metrics, run_crawler


class CrawlerTests(unittest.IsolatedAsyncioTestCase):
    def test_navigation_boundaries(self):
        self.assertTrue(safe_url('https://www3.eoriginal.ro/search/result','autototal'))
        for url in ('http://www3.eoriginal.ro/search/result','https://evil.test/search/result',
                    'https://www3.eoriginal.ro/search/cart','https://user@www3.eoriginal.ro/search/result',
                    'https://www3.eoriginal.ro:1234/search/result'):
            self.assertFalse(safe_url(url,'autototal'), url)
        self.assertFalse(safe_url('https://ro.e-cat.intercars.eu/ro/checkout','intercars'))

    def test_unreviewed_price_and_accuracy_not_counted_as_confirmed(self):
        result = metrics([{'requested_item':'filtru aer','price_ron':123,'needs_price_review':True}],{'air_filter','engine_oil'})
        self.assertEqual(result['priced_products'],0)
        self.assertEqual(result['missing_kinds'],['engine_oil'])
        self.assertIsNone(result['accuracy_percent'])

    async def test_failed_crawl_closes_only_its_own_context(self):
        original = SimpleNamespace(storage_state=AsyncMock(return_value={'cookies':[],'origins':[]}),close=AsyncMock())
        page = SimpleNamespace(goto=AsyncMock(side_effect=RuntimeError('supplier denied request')))
        owned = SimpleNamespace(new_page=AsyncMock(return_value=page),close=AsyncMock())
        browser = SimpleNamespace(contexts=[original],new_context=AsyncMock(return_value=owned),close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))
        context = AsyncMock()
        context.__aenter__.return_value=playwright
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'result.json'
            with patch('playwright.async_api.async_playwright',return_value=context):
                await run_crawler(40,'WBA00000000000002','revizie',[],output)
            result=json.loads(output.read_text())
            self.assertEqual(result['status'],'error')
            self.assertEqual(set(result['sources']),{'autototal','intercars'})
            self.assertEqual(result['products'],[])
        owned.close.assert_awaited_once()
        original.close.assert_not_awaited()
        browser.close.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
