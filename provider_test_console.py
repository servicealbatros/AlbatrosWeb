"""Temporary, administrator-granted LOGIN-ONLY console for the free Render test.

No cookies/passwords are exported. No arbitrary navigation, product selection,
checkout or shell commands are exposed. Disabled outside the explicit test flag.
"""
import asyncio
import base64
import hmac
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

router = APIRouter()
GRANTS, SESSIONS = {}, {}
LOCK = asyncio.Lock()
PROVIDERS = {'DSM': 'https://apps.dsmpro.ro/',
             'Inter Cars': 'https://ro.e-cat.intercars.eu/',
             'AutoTotal': 'https://www3.eoriginal.ro/',
             'Haynes': 'https://www.workshopdata.com/touch/site/layout/makesOverview'}
DOMAINS = ('dsmpro.ro', 'q-service.ro', 'intercars.eu', 'eoriginal.ro', 'workshopdata.com', 'microsoftonline.com')
_playwright = _browser = _page = None


def enabled():
    if os.getenv('ALBATROS_PROVIDER_TEST_CONSOLE') != 'true':
        raise HTTPException(404, 'Test console disabled')


def admin(request):
    enabled()
    expected = os.getenv('ADMIN_API_TOKEN', '')
    supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(403, 'Administrator required')


def same_origin(request):
    if urlparse(request.headers.get('origin', '')).netloc != request.headers.get('host'):
        raise HTTPException(403, 'Same-origin action required')


def session(request):
    enabled()
    token = request.cookies.get('albatros_provider_test', '')
    if SESSIONS.get(token, 0) < time.time():
        raise HTTPException(403, 'Access expired; request a new administrator grant')


def allowed_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ''
    return parsed.scheme == 'https' and not parsed.username and not parsed.password and any(
        host == domain or host.endswith('.' + domain) for domain in DOMAINS)


@router.post('/api/admin/provider-test-grant')
async def grant(request: Request):
    admin(request)
    token = secrets.token_urlsafe(32)
    GRANTS[token] = time.time() + 120
    # URL fragment is consumed via POST; it never goes into server request logs.
    return {'url': str(request.base_url).rstrip('/') + '/provider-auth#' + token, 'expires_in': 120}


class Unlock(BaseModel):
    token: str = Field(max_length=100)


@router.post('/api/provider-auth/session')
async def unlock(body: Unlock, request: Request):
    enabled()
    same_origin(request)
    if GRANTS.pop(body.token, 0) < time.time():
        raise HTTPException(403, 'Invalid or expired grant')
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = time.time() + 1800
    response = JSONResponse({'ok': True})
    response.set_cookie('albatros_provider_test', token, httponly=True, secure=True,
                        samesite='strict', max_age=1800)
    response.headers['Cache-Control'] = 'no-store'
    return response


@router.get('/provider-auth')
async def panel():
    enabled()
    return FileResponse(Path(__file__).parent / 'static/provider_test_console.html',
                        headers={'Cache-Control': 'no-store'})


async def context():
    global _playwright, _browser
    if _browser is None or not _browser.is_connected():
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=30000)
    return _browser.contexts[0]


async def snapshot():
    if _page is None or _page.is_closed():
        return {'page_open': False}
    if not allowed_url(_page.url):
        return {'page_open': True, 'blocked_host': urlparse(_page.url).hostname,
                'forms': [], 'image': ''}
    # Values and cookies are deliberately omitted. Buttons are limited to
    # login/verification forms, never shopping/cart operations.
    forms = await _page.locator('form').evaluate_all("""forms=>forms.map((f,index)=>{
      const shown=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
      const login=/autent|login|log in|sign in|conect|password|parol|utilizator|username|verification|verify|otp|cod.*confirm/i.test(f.innerText)
         ||!!f.querySelector('input[type=password]');
      if(!login||!shown(f))return null;
      const fields=[...f.querySelectorAll('input')].map((e,i)=>({index:i,type:e.type,
        label:e.labels?.[0]?.innerText||e.placeholder||e.name||e.type,visible:shown(e)}))
        .filter(e=>e.visible&&['text','email','password','tel','number'].includes(e.type));
      const buttons=[...f.querySelectorAll('button,input[type=submit]')].map((e,i)=>({index:i,
        text:e.innerText||e.value||'Autentificare',visible:shown(e)})).filter(e=>e.visible&&
        /autent|login|log in|sign in|conect|intr|continu|next|verify|verific|confirm|submit|trimite/i.test(e.text));
      return {index,fields,buttons}; }).filter(Boolean)""")
    shot = await _page.screenshot(type='jpeg', quality=45, timeout=20000)
    return {'page_open': True, 'host': urlparse(_page.url).hostname,
            'forms': forms, 'image': 'data:image/jpeg;base64,' + base64.b64encode(shot).decode(),
            'login_form_present': bool(forms)}


class Provider(BaseModel):
    provider: str


@router.post('/api/provider-auth/open')
async def open_provider(body: Provider, request: Request):
    global _page
    session(request)
    same_origin(request)
    if body.provider not in PROVIDERS:
        raise HTTPException(422, 'Unknown provider')
    async with LOCK:
        ctx = await context()
        if _page is not None and not _page.is_closed():
            await _page.close()
        _page = await ctx.new_page()
        try:
            await _page.goto(PROVIDERS[body.provider], wait_until='domcontentloaded', timeout=45000)
        except Exception:
            pass  # A loaded login form can still be usable after a navigation timeout.
        return await snapshot()


@router.get('/api/provider-auth/state')
async def state(request: Request):
    session(request)
    async with LOCK:
        return await snapshot()


class Submit(BaseModel):
    form: int = Field(ge=0)
    button: int = Field(ge=0)
    fields: dict[str, str]


@router.post('/api/provider-auth/submit')
async def submit(body: Submit, request: Request):
    session(request)
    same_origin(request)
    async with LOCK:
        current = await snapshot()
        form = next((form for form in current.get('forms', []) if form['index'] == body.form), None)
        if not form or body.button not in {button['index'] for button in form['buttons']}:
            raise HTTPException(403, 'Only visible login/verification actions are allowed')
        valid_fields = {str(field['index']) for field in form['fields']}
        if not set(body.fields).issubset(valid_fields) or any(len(value) > 512 for value in body.fields.values()):
            raise HTTPException(422, 'Invalid login fields')
        locator = _page.locator('form').nth(body.form)
        for index, value in body.fields.items():
            await locator.locator('input').nth(int(index)).fill(value)
        await locator.locator('button,input[type=submit]').nth(body.button).click(timeout=15000)
        await asyncio.sleep(2)
        return await snapshot()
