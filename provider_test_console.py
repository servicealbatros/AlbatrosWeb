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
import threading
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from playwright_lifecycle import CdpDriver

router = APIRouter()
GRANTS, SESSIONS = {}, {}
LOCK = asyncio.Lock()
ACCESS_LOCK = threading.RLock()
MAX_GRANTS = 1024
MAX_SESSIONS = 1024
DRIVER = CdpDriver()
PROVIDERS = {'DSM': 'https://apps.dsmpro.ro/',
             'Inter Cars': 'https://ro.e-cat.intercars.eu/',
             'AutoTotal': 'https://www3.eoriginal.ro/',
             'Haynes': 'https://www.workshopdata.com/touch/site/layout/makesOverview'}
DOMAINS = ('dsmpro.ro', 'q-service.ro', 'intercars.eu', 'eoriginal.ro', 'workshopdata.com', 'microsoftonline.com')
_browser = _page = None
_page_id = ''
_provider_pages = {}


def check_page(page_id):
    if not _page_id or not hmac.compare_digest(page_id, _page_id):
        raise HTTPException(409, 'Furnizorul s-a schimbat. Actualizează ecranul înainte să continui.')


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


def cleanup_access_tokens(now=None):
    now = time.time() if now is None else now
    with ACCESS_LOCK:
        for entries in (GRANTS, SESSIONS):
            for token, deadline in list(entries.items()):
                if deadline <= now:
                    del entries[token]


def session(request):
    enabled()
    token = request.cookies.get('albatros_provider_test', '')
    with ACCESS_LOCK:
        cleanup_access_tokens()
        if token not in SESSIONS:
            raise HTTPException(403, 'Access expired; request a new administrator grant')


def allowed_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ''
    return parsed.scheme == 'https' and not parsed.username and not parsed.password and any(
        host == domain or host.endswith('.' + domain) for domain in DOMAINS)


def provider_page(url, provider):
    families = {'DSM': ('dsmpro.ro', 'q-service.ro'), 'Inter Cars': ('intercars.eu',),
                'AutoTotal': ('eoriginal.ro',), 'Haynes': ('workshopdata.com',)}
    host = urlparse(url).hostname or ''
    return allowed_url(url) and any(host == d or host.endswith('.' + d) for d in families[provider])


def captcha_url(url):
    parsed = urlparse(url)
    host = parsed.hostname or ''
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and any(host == domain or host.endswith('.' + domain)
                    for domain in ('google.com', 'recaptcha.net'))
            and parsed.path.startswith('/recaptcha/'))


async def captcha_regions():
    if _page is None or _page.is_closed() or not allowed_url(_page.url):
        return []
    regions = []
    for frame in await _page.locator('iframe').all():
        src = await frame.get_attribute('src') or ''
        if captcha_url(src) and await frame.is_visible():
            box = await frame.bounding_box()
            if box and box['width'] > 0 and box['height'] > 0:
                regions.append(box)
    return regions


def inside_region(x, y, regions):
    return any(r['x'] <= x < r['x'] + r['width']
               and r['y'] <= y < r['y'] + r['height'] for r in regions)


@router.post('/api/admin/provider-test-grant')
async def grant(request: Request):
    admin(request)
    token = secrets.token_urlsafe(32)
    with ACCESS_LOCK:
        cleanup_access_tokens()
        if len(GRANTS) >= MAX_GRANTS:
            raise HTTPException(503, 'Too many active grants; retry after expiry')
        GRANTS[token] = time.time() + 120
    # URL fragment is consumed via POST; it never goes into server request logs.
    return {'url': str(request.base_url).rstrip('/') + '/provider-auth#' + token, 'expires_in': 120}


class Unlock(BaseModel):
    token: str = Field(max_length=100)


@router.post('/api/provider-auth/session')
async def unlock(body: Unlock, request: Request):
    enabled()
    same_origin(request)
    token = secrets.token_urlsafe(32)
    with ACCESS_LOCK:
        cleanup_access_tokens()
        if len(SESSIONS) >= MAX_SESSIONS:
            raise HTTPException(503, 'Too many active sessions; retry after expiry')
        if GRANTS.pop(body.token, 0) <= time.time():
            raise HTTPException(403, 'Invalid or expired grant')
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
    global _browser, _page, _page_id
    try:
        ctx = await DRIVER.context()
    except BaseException:
        # Do not retain stale page/connection object graphs after a failed reconnect.
        _browser = _page = None
        _page_id = ''
        _provider_pages.clear()
        raise
    if _browser is not DRIVER.browser:
        _browser = DRIVER.browser
        _provider_pages.clear()
        _page, _page_id = None, ''
    return ctx


async def shutdown_browser():
    global _browser, _page, _page_id
    try:
        await DRIVER.shutdown()
    finally:
        _browser = _page = None
        _page_id = ''
        _provider_pages.clear()
        with ACCESS_LOCK:
            GRANTS.clear()
            SESSIONS.clear()


async def snapshot():
    if _page is None or _page.is_closed():
        return {'page_open': False}
    if not allowed_url(_page.url):
        return {'page_open': True, 'blocked_host': urlparse(_page.url).hostname,
                'forms': [], 'image': ''}
    # Values and cookies are deliberately omitted. Buttons are limited to
    # login/verification forms, never shopping/cart operations.
    forms = await _page.locator('body').evaluate("""body=>{
      let roots=[...body.querySelectorAll('form')].map((f,index)=>({f,index}));
      const shown=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
      const identity=e=>shown(e)&&(e.type==='password'||e.type==='email'||
         /email|username|utilizator|login/i.test(e.name+' '+e.placeholder+' '+(e.labels?.[0]?.innerText||'')));
      if(!roots.some(({f})=>shown(f)&&[...f.querySelectorAll('input')].some(identity))
         &&[...body.querySelectorAll('input')].some(identity)
         &&/autent|sign in|log in|login|conect|password|parol/i.test(body.innerText))roots.push({f:body,index:-1});
      return roots.map(({f,index})=>{
      const login=/autent|login|log in|sign in|conect|password|parol|utilizator|username|verification|verify|otp|cod.*confirm/i.test(f.innerText)
         ||!!f.querySelector('input[type=password]')
         ||(/autent|login|log in|sign in|conect/i.test(body.innerText)
            &&[...f.querySelectorAll('input')].some(identity));
      if(!login||!shown(f))return null;
      const fields=[...f.querySelectorAll('input')].map((e,i)=>({index:i,type:e.type,
        label:e.labels?.[0]?.innerText||e.placeholder||e.name||e.type,visible:shown(e)}))
        .filter(e=>e.visible&&['text','email','password','tel','number'].includes(e.type));
      const newDevice=/dispozitiv nou|dispozitiv[/]browser nou|new device/i.test(body.innerText)
        &&fields.length===1&&/denumire|device.*name/i.test(fields[0].label);
      const buttons=[...f.querySelectorAll('button,input[type=submit]')].map((e,i)=>({index:i,
        text:e.innerText||e.value||'Autentificare',visible:shown(e)})).filter(e=>e.visible&&
        (/autent|login|log in|sign in|conect|intr|continu|next|verify|verific|confirm|submit|trimite/i.test(e.text)
         ||(newDevice&&/^(salvare|save)$/i.test(e.text.trim())))&&
        !/comand|co[sș]|cart|checkout|cump|pl[aă]t|order|buy|register|cre.*cont/i.test(e.text));
      return {index,fields,buttons}; }).filter(Boolean)}""")
    from playwright.async_api import TimeoutError as ScreenshotTimeout
    try:
        shot = await _page.screenshot(type='png', animations='disabled', timeout=5000)
    except ScreenshotTimeout:
        # A delayed font/image must not prevent a valid login form submission.
        debugger = None
        try:
            debugger = await _page.context.new_cdp_session(_page)
            capture = await debugger.send('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': False})
            shot = base64.b64decode(capture['data'])
        except Exception:
            shot = None
        finally:
            if debugger is not None:
                await debugger.detach()
    navigation = await _page.locator('a,button').evaluate_all("""nodes=>nodes.map((e,index)=>({index,
      text:(e.innerText||'').trim(),visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)}))
      .filter(e=>e.visible&&/^(autentificare|conectare|login|log in|sign in|contul meu|my account)$/i.test(e.text))""")
    captcha = _page.locator('[name="g-recaptcha-response"]')
    captcha_present = await captcha.count() > 0
    captcha_complete = await captcha.evaluate_all('nodes=>nodes.some(e=>!!e.value.trim())') if captcha_present else False
    notice = await _page.locator('[role=alert],.alert-danger,.invalid-feedback').all_text_contents()
    dsm_saved = False
    if not forms and os.getenv('ALBATROS_PROVIDER_STATE_KEY'):
        from dsm_session_vault import DsmSessionVault, trusted_landing
        if trusted_landing(_page.url) and await _page.locator('a[href*="logout"],a[href*="logoff"],form[action*="logout"]').count():
            dsm_saved = DsmSessionVault(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], os.environ['ALBATROS_PROVIDER_STATE_KEY']).save(await _page.context.cookies(), _page.url)
    return {'page_open': True, 'page_id': _page_id, 'host': urlparse(_page.url).hostname,
            'forms': forms, 'image': 'data:image/png;base64,' + base64.b64encode(shot).decode() if shot else '',
            'screenshot_unavailable': shot is None,
            'notice': ' '.join(notice)[:500], 'dsm_session_saved': dsm_saved,
            'login_form_present': bool(forms), 'auth_navigation': [] if forms else navigation,
            'captcha_present': captcha_present, 'captcha_complete': captcha_complete,
            'captcha_regions': await captcha_regions()}


class PageAction(BaseModel):
    page_id: str = Field(min_length=1, max_length=100)


class CaptchaClick(PageAction):
    x: float = Field(ge=0, le=10000, allow_inf_nan=False)
    y: float = Field(ge=0, le=10000, allow_inf_nan=False)


@router.post('/api/provider-auth/captcha-click')
async def captcha_click(body: CaptchaClick, request: Request):
    # This is a human-operated surface, not automatic CAPTCHA solving. Only
    # current visible reCAPTCHA iframe coordinates can receive a click.
    session(request)
    same_origin(request)
    async with LOCK:
        check_page(body.page_id)
        regions = await captcha_regions()
        if not inside_region(body.x, body.y, regions):
            raise HTTPException(403, 'Apasă numai în verificarea CAPTCHA din imagine.')
        try:
            await _page.mouse.click(body.x, body.y)
            await asyncio.sleep(1)
            return await snapshot()
        except Exception:
            raise HTTPException(502, 'Verificarea nu a răspuns. Actualizează ecranul.') from None


class Provider(BaseModel):
    provider: str


@router.post('/api/provider-auth/open')
async def open_provider(body: Provider, request: Request):
    global _page, _page_id
    session(request)
    same_origin(request)
    if body.provider not in PROVIDERS:
        raise HTTPException(422, 'Unknown provider')
    async with LOCK:
        ctx = await context()
        cached = _provider_pages.get(body.provider)
        if cached and not cached[0].is_closed():
            _page, _page_id = cached
        else:
            _page = next((p for p in ctx.pages if not p.is_closed() and provider_page(p.url, body.provider)), None)
            existing = _page is not None
            if not existing:
                _page = await ctx.new_page()
            _page_id = secrets.token_urlsafe(24)
            _provider_pages[body.provider] = (_page, _page_id)
            if not existing:
                try:
                    await _page.goto(PROVIDERS[body.provider], wait_until='domcontentloaded', timeout=45000)
                except Exception:
                    pass  # A loaded login form can still be usable after a navigation timeout.
        from provider_sessions import upstream_error
        if await upstream_error(_page):
            try:
                await _page.reload(wait_until='domcontentloaded', timeout=30000)
            except Exception:
                pass
        return await snapshot()


@router.get('/api/provider-auth/state')
async def state(request: Request):
    session(request)
    async with LOCK:
        return await snapshot()


@router.get('/api/provider-auth/reconnection-status')
async def reconnection_status(request: Request):
    session(request)
    import json
    from provider_sessions import ProviderLoginVault
    from dsm_session_vault import DsmLoginVault
    profile, key = os.getenv('ALBATROS_BROWSER_PROFILE_DIR'), os.getenv('ALBATROS_PROVIDER_STATE_KEY')
    if not profile or not key:
        return {'providers': {p: {'configured': False, 'status': 'storage_unavailable'} for p in PROVIDERS}}
    path = Path(os.environ['ALBATROS_DATA_DIR']) / 'provider_session_health.json'
    try:
        health = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError, ValueError):
        health = {}
    return {'providers': {p: {'configured': (DsmLoginVault(profile, key) if p == 'DSM' else ProviderLoginVault(profile, key, p)).path.exists(),
                             'status': str(health.get(p, {}).get('status') or 'unverified')}
                          for p in PROVIDERS}}


class Submit(PageAction):
    form: int = Field(ge=-1)
    button: int = Field(ge=0)
    fields: dict[str, str]
    prepare_only: bool = False
    remember_dsm: bool = False
    remember_provider: bool = False


@router.post('/api/provider-auth/submit')
async def submit(body: Submit, request: Request):
    session(request)
    same_origin(request)
    async with LOCK:
        check_page(body.page_id)
        current = await snapshot()
        form = next((form for form in current.get('forms', []) if form['index'] == body.form), None)
        if not form or body.button not in {button['index'] for button in form['buttons']}:
            raise HTTPException(403, 'Only visible login/verification actions are allowed')
        valid_fields = {str(field['index']) for field in form['fields']}
        if not set(body.fields).issubset(valid_fields) or any(len(value) > 512 for value in body.fields.values()):
            raise HTTPException(422, 'Invalid login fields')
        locator = _page.locator('body') if body.form == -1 else _page.locator('form').nth(body.form)
        try:
            fields_changed = False
            for index, value in body.fields.items():
                field = locator.locator('input').nth(int(index))
                if await field.input_value() != value:
                    fields_changed = True
                    await field.fill(value, timeout=15000)
            # Human verification is left intact: unchanged credentials must
            # not fire input events again after the CAPTCHA has been solved.
            captcha = _page.locator('[name="g-recaptcha-response"]')
            captcha_present = await captcha.count() > 0
            captcha_pending = (captcha_present and not
                await captcha.evaluate_all('nodes=>nodes.some(e=>!!e.value.trim())'))
            if body.remember_provider and not body.prepare_only:
                from provider_sessions import ProviderLoginVault, DOMAINS, provider_url
                provider = next((p for p in DOMAINS if provider_url(p, _page.url)), None)
                password_field = next((str(f['index']) for f in form['fields'] if f['type'] == 'password'), None)
                username_fields = [str(f['index']) for f in form['fields'] if f['type'] in {'text', 'email'}]
                if provider and password_field is not None and len(username_fields) == 1:
                    username, password = body.fields.get(username_fields[0]), body.fields.get(password_field)
                    if username and password:
                        key = os.environ['ALBATROS_PROVIDER_STATE_KEY']
                        profile = os.environ['ALBATROS_BROWSER_PROFILE_DIR']
                        if provider == 'DSM':
                            from dsm_session_vault import DsmLoginVault
                            DsmLoginVault(profile, key).save(username, password)
                        else:
                            ProviderLoginVault(profile, key, provider).save(username, password, _page.url)
            if body.prepare_only or captcha_pending or (captcha_present and fields_changed):
                state = await snapshot()
                state['captcha_pending'] = captcha_pending
                state['prepared_only'] = True
                return state
            await locator.locator('button,input[type=submit]').nth(body.button).click(timeout=15000)
        except Exception:
            # Don't let a browser exception include entered credentials in
            # an application traceback or return them to the console.
            raise HTTPException(502, 'Formularul nu a răspuns. Actualizează ecranul și verifică autentificarea.') from None
        await asyncio.sleep(2)
        result = await snapshot()
        if body.remember_dsm and result.get('dsm_session_saved'):
            from dsm_session_vault import DsmLoginVault
            password_field = next((str(f['index']) for f in form['fields'] if f['type'] == 'password'), None)
            username_field = next((str(f['index']) for f in form['fields'] if f['type'] in {'text','email'}), None)
            if password_field and username_field and body.fields.get(password_field) and body.fields.get(username_field):
                DsmLoginVault(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], os.environ['ALBATROS_PROVIDER_STATE_KEY']).save(body.fields[username_field], body.fields[password_field])
                result['dsm_auto_login_configured'] = True
        return result


class DsmCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=512)


@router.post('/api/provider-auth/dsm-auto-login')
async def configure_dsm_login(body: DsmCredentials, request: Request):
    session(request)
    same_origin(request)
    from dsm_session_vault import DsmLoginVault
    key = os.getenv('ALBATROS_PROVIDER_STATE_KEY')
    if not key:
        raise HTTPException(503, 'Stocarea criptată nu este configurată.')
    DsmLoginVault(os.environ['ALBATROS_BROWSER_PROFILE_DIR'], key).save(body.username, body.password)
    return {'ok': True}


class SupplierCredentials(DsmCredentials):
    provider: str


@router.post('/api/provider-auth/auto-login')
async def configure_supplier_login(body: SupplierCredentials, request: Request):
    session(request)
    same_origin(request)
    if body.provider not in PROVIDERS:
        raise HTTPException(422, 'Furnizor necunoscut.')
    key, profile = os.getenv('ALBATROS_PROVIDER_STATE_KEY'), os.getenv('ALBATROS_BROWSER_PROFILE_DIR')
    if not key or not profile:
        raise HTTPException(503, 'Stocarea criptată nu este configurată.')
    if body.provider == 'DSM':
        from dsm_session_vault import DsmLoginVault
        DsmLoginVault(profile, key).save(body.username, body.password)
    else:
        from provider_sessions import ProviderLoginVault
        # Explicit supplier-level authorization. The keeper still requires an
        # owned HTTPS domain and a single, visible sign-in form before filling.
        ProviderLoginVault(profile, key, body.provider).save(body.username, body.password,
            PROVIDERS[body.provider], strict_location=False)
    return {'ok': True, 'provider': body.provider}


class LoginLink(PageAction):
    index: int = Field(ge=0)


@router.post('/api/provider-auth/login-link')
async def login_link(body: LoginLink, request: Request):
    session(request)
    same_origin(request)
    async with LOCK:
        check_page(body.page_id)
        current = await snapshot()
        if body.index not in {item['index'] for item in current.get('auth_navigation', [])}:
            raise HTTPException(403, 'Only displayed authentication links are allowed')
        locator = _page.locator('a,button').nth(body.index)
        href = await locator.get_attribute('href')
        if href and href.startswith(('http:', 'https:')) and not allowed_url(href):
            raise HTTPException(403, 'Unverified authentication destination')
        await locator.click(timeout=15000)
        await asyncio.sleep(2)
        return await snapshot()


@router.post('/api/provider-auth/prepare-offer')
async def prepare_offer(request: Request):
    session(request)
    same_origin(request)
    async with LOCK:
        ctx = await context()
        for name in ('DSM', 'Inter Cars', 'AutoTotal'):
            url = PROVIDERS[name]
            if any(provider_page(page.url, name) for page in ctx.pages):
                continue
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception:
                pass
        return {'ok': True, 'message': 'Filele pentru test sunt deschise. Autentificarea trebuie verificată prin accesul efectiv la catalog.'}
