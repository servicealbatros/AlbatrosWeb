"""Bounded rendered-page crawler. Isolated context; no orders or quote writes."""
import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from autototal_request_worker import (_post_helper_js, _oe_vin_targets, _plain,
    _oe_option_allowed, _revision_product_kind, _required_product_kinds,
    _choose_engine_oil_card, _confirmed_oil_requirements)


def safe_url(url, supplier):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None,443)
            or re.search(r'/(?:cart|checkout|order|buy|delete)(?:/|$)',parsed.path,re.I)):
        return False
    if supplier == 'autototal':
        return parsed.hostname == 'www3.eoriginal.ro' and (
            parsed.path == '/' or parsed.path.startswith(('/search/', '/auto/vin/', '/uleiuri/')))
    return parsed.hostname == 'ro.e-cat.intercars.eu' and (
        parsed.path == '/ro/' or parsed.path.startswith(('/ro/p/', '/ro/c/')))


def metrics(products, required):
    found = {_revision_product_kind(p.get('requested_item') or p.get('description')) for p in products}
    return {'found_kinds': sorted(found), 'missing_kinds': sorted(required - found),
            'priced_products': sum(bool(p.get('price_ron')) and not p.get('needs_price_review') for p in products),
            'oe_verified_products': sum(bool(p.get('vin_exact_oe_cross_reference')) for p in products),
            'accuracy_percent': None, 'human_review_required': True}


class Crawl:
    def __init__(self, page):
        self.page = page
        self.pages = []
        self.requests = 0

    async def visit(self, url, supplier):
        if not safe_url(url, supplier) or len(self.pages) >= 24:
            raise RuntimeError('unsafe_url_or_page_limit')
        response = await self.page.goto(url, wait_until='domcontentloaded', timeout=25000)
        self.requests += 1
        self.pages.append({'supplier': supplier, 'path': urlparse(url).path,
                           'http_status': response.status if response else None})
        if not safe_url(self.page.url, supplier):
            raise RuntimeError('login_or_unapproved_redirect')
        if response and response.status in {401,403,429}:
            raise RuntimeError('supplier_access_denied_'+str(response.status))
        if await self.page.locator('input[type=password]:visible').count():
            raise RuntimeError('supplier_login_required')
        # A permanently embedded reCAPTCHA badge is not an active challenge.
        if await self.page.locator('iframe[src*="bframe"]:visible,iframe[title*="challenge"]:visible,iframe[src*="challenges.cloudflare.com"]:visible').count():
            raise RuntimeError('human_verification_required')

    async def post(self, route, payload):
        if route not in {'/search/searchVin','/car/get-lcar-categ','/search/doSearch'}:
            raise RuntimeError('endpoint_not_read_only_allowlisted')
        if not safe_url(self.page.url, 'autototal'):
            raise RuntimeError('wrong_supplier_origin')
        self.requests += 1
        return await self.page.evaluate('''async ({route,payload,helper})=>{
            const post=eval('('+helper+')');return (await post(route,payload)).data;
        }''', {'route':route,'payload':payload,'helper':_post_helper_js()})

    async def auto_search(self, query):
        await self.visit('https://www3.eoriginal.ro/#/', 'autototal')
        section = await self.page.evaluate('()=>typeof section!=="undefined"?section:0')
        data = await self.post('/search/doSearch', {'search':query,'searchType':0,'section':section,
            'ref':'crawler','autoSelection':1,'useAiSearch':False})
        result = data.get('result') or {}
        url = next((s for s in (data.get('redirect'),result.get('result'),result.get('redirect'))
                    if isinstance(s,str) and safe_url(s,'autototal')), None)
        if not url:
            return []
        await self.visit(url,'autototal')
        try:
            await self.page.wait_for_function("[...document.querySelectorAll('.article-card')].some(c=>/Pret vanzare.*[0-9]/s.test(c.innerText))", timeout=12000)
        except Exception:
            pass
        return await self.page.locator('.article-card').evaluate_all("nodes=>nodes.slice(0,30).map(c=>({title:c.querySelector('.card-title')?.innerText||'',text:c.innerText||'',url:location.href}))")


async def run_crawler(request_id, vin, query, seeds, output):
    started = time.monotonic()
    required = _required_product_kinds(query)
    report = {'request_id':request_id,'vin':vin,'query':query,'mode':'crawler',
              'status':'running','started_at':datetime.now().isoformat(timespec='seconds'),
              'products':[],'sources':{},'pages':[], 'bounds':{'max_pages':24,'max_oe_codes':12,'max_seconds':300}}
    report['seed_source']='VIN-confirmed OE evidence from existing worker run; AutoTotal VIN is also queried independently'
    def save():
        report['elapsed_seconds'] = round(time.monotonic()-started,2)
        report.update(metrics(report['products'], required))
        temporary = Path(str(output)+'.tmp')
        temporary.write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
        temporary.replace(output)
    save()
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp('http://127.0.0.1:9222')
        # Authentication stays in memory; existing worker pages are untouched.
        state = await browser.contexts[0].storage_state()
        context = await browser.new_context(storage_state=state)
        page = await context.new_page()
        crawler = Crawl(page)
        candidates = list(seeds)
        try:
            async with asyncio.timeout(300):
                try:
                    await crawler.visit('https://www3.eoriginal.ro/#/', 'autototal')
                    identification = (await crawler.post('/search/searchVin',{'vin':vin,'return_car':1})).get('result') or {}
                    link = identification.get('car_link')
                    if not link:
                        raise RuntimeError('vin_not_identified')
                    await crawler.visit(link,'autototal')
                    for catalogue in list((identification.get('lx_catalog') or {}).values())[:1]:
                        groups = {}
                        for target in _oe_vin_targets(query):
                            group_id = target['group_id']
                            if group_id not in groups:
                                data = await crawler.post('/car/get-lcar-categ',{'t_id':'0','c_id':catalogue['catalog'],'s_id':catalogue['ssd'],'g_id':group_id,'vin':vin})
                                groups[group_id] = data.get('result') or {}
                            for group in groups[group_id].get('groups') or []:
                                for unit in group.get('units') or []:
                                    position = _plain(str(group.get('name') or '')+' '+str(unit.get('name') or ''))
                                    if target.get('require_position_context'):
                                        if target.get('front') and not re.search(r'\b(front|fata|anterior)\b',position):
                                            continue
                                        if target.get('rear') and not re.search(r'\b(rear|spate|posterior)\b',position):
                                            continue
                                    if target.get('left') or target.get('right'):
                                        continue  # Side-specific diagrams require a reviewed side match.
                                    for part in unit.get('details') or []:
                                        name = _plain(part.get('name'))
                                        oil_pan_plug = (_revision_product_kind(target['requested_item']) == 'oil_drain_plug'
                                            and re.match(r'^screw plug\b', name) and re.search(r'\boil (pan|sump)\b',position))
                                        if part.get('oem') and (oil_pan_plug or any(term in name for term in target['terms'])):
                                            candidates.append({'requested_item':target['requested_item'],'oe_code':part['oem'],'description':part['name']})
                    seen = set()
                    for candidate in candidates[:12]:
                        code = re.sub(r'\s+','',str(candidate.get('oe_code') or candidate.get('manufacturer_ref') or ''))
                        if not code or code in seen:
                            continue
                        seen.add(code)
                        for card in await crawler.auto_search(code):
                            compact = re.sub(r'[^A-Z0-9]','',card['title'].upper())
                            # Exact original reference must be printed on the card.
                            if re.sub(r'[^A-Z0-9]','',code.upper()) not in compact:
                                continue
                            if not _oe_option_allowed(candidate,card['title'],'OE'):
                                continue
                            price = re.search(r'Pret vanzare \(cu TVA\)\s*([\d]+(?:[.,]\d{1,2})?)',card['text'])
                            if not price:
                                continue
                            report['products'].append({**candidate,'manufacturer_ref':code,'brand':'OE',
                                'price_ron':float(price.group(1).replace(',','.')),'description':card['title'],
                                'source':'AutoTotal crawler','vin_exact_oe_cross_reference':True})
                        save()
                    requirements = _confirmed_oil_requirements(request_id,vin)
                    make = str(identification.get('td_catalog') or '')
                    # OEM name comes from the request evidence, never a guessed engine.
                    oe_brand = next((s for s in ('BMW','AUDI','VW','MERCEDES') if s in query.upper()), '')
                    if requirements and oe_brand and 'engine_oil' in required:
                        requirements.update(oe_brand=oe_brand,oe_only=True)
                        grades = [r['value'] for option in requirements['engine_oil_options'] for r in option.get('grades') or []]
                        approvals = [r['value'] for option in requirements['engine_oil_options'] for r in option.get('classifications') or []]
                        oil = _choose_engine_oil_card(await crawler.auto_search(' '.join([oe_brand]+grades[:1]+approvals[:1])),requirements)
                        if oil:
                            oil['source']='AutoTotal crawler — normă confirmată TecRMI'
                            report['products'].append(oil)
                    report['sources']['autototal']={'status':'finished'}
                except Exception as error:
                    report['sources']['autototal']={'status':'error','error':str(error)[:300]}
                save()
                try:
                    await crawler.visit('https://ro.e-cat.intercars.eu/ro/','intercars')
                    if not candidates:
                        raise RuntimeError('no_verified_oe_codes_available_for_search')
                    for candidate in candidates[:12]:
                        code = str(candidate.get('oe_code') or candidate.get('manufacturer_ref') or '')
                        if not code:
                            continue
                        field = None
                        for node in await page.locator('input:visible').all():
                            label = (await node.get_attribute('placeholder') or '')+' '+(await node.get_attribute('aria-label') or '')
                            if re.search(r'c[aă]utare produse|search products|search by name|nume, index',label,re.I) and not re.search(r'vin|category|categorie',label,re.I):
                                field = node
                                break
                        if field is None:
                            raise RuntimeError('product_search_not_available')
                        await field.fill(code)
                        await field.press('Enter')
                        await asyncio.sleep(2)
                        links = await page.locator('a[href*="/ro/p/"]').evaluate_all("nodes=>[...new Set(nodes.map(a=>a.href))].slice(0,2)")
                        for link in links:
                            await crawler.visit(link,'intercars')
                            text = await page.locator('body').inner_text()
                            title = await page.locator('h1').first.inner_text() if await page.locator('h1').count() else ''
                            if not title or not _oe_option_allowed(candidate,title,'OE'):
                                continue
                            compact = re.sub(r'[^A-Z0-9]','',text.upper())
                            if re.sub(r'[^A-Z0-9]','',code.upper()) not in compact:
                                continue
                            price = re.search(r'(\d+(?:[.,]\d{2}))\s*RON',text)
                            report['products'].append({**candidate,'manufacturer_ref':code,
                                'price_ron':float(price.group(1).replace(',','.')) if price else None,
                                'source':'Inter Cars crawler','vin_exact_oe_cross_reference':True,
                                'description':title,
                                'needs_price_review':True})
                        await crawler.visit('https://ro.e-cat.intercars.eu/ro/','intercars')
                        save()
                    report['sources']['intercars']={'status':'finished'}
                except Exception as error:
                    report['sources']['intercars']={'status':'error','error':str(error)[:300]}
        except TimeoutError:
            report['sources']['timeout']={'status':'error','error':'Crawler stopped at 300 seconds'}
        finally:
            report['pages']=crawler.pages
            report['explicit_catalogue_requests']=crawler.requests
            report['status']='partial' if report['products'] else 'error'
            report['finished_at']=datetime.now().isoformat(timespec='seconds')
            save()
            # Only the context created by this crawler is closed.
            await context.close()
