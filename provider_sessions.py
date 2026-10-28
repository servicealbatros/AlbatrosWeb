"""Encrypted supplier credentials and bounded sign-in to trusted login forms."""
import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from cryptography.fernet import Fernet

DOMAINS = {'DSM': ('dsmpro.ro', 'servicemanager.q-service.ro'),
           'Inter Cars': ('intercars.eu',), 'AutoTotal': ('eoriginal.ro',),
           'Haynes': ('workshopdata.com',)}
AUTOTOTAL_HOME = 'https://www3.eoriginal.ro/'
AUTOTOTAL_LOGIN = 'https://www3.eoriginal.ro/login'


def provider_url(provider, url):
    p = urlparse(url)
    return (provider in DOMAINS and p.scheme == 'https' and not p.username and not p.password
            and any(p.hostname == d or (p.hostname or '').endswith('.' + d) for d in DOMAINS[provider]))


def login_identity(url):
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, '', '', ''))


class ProviderLoginVault:
    def __init__(self, profile, key, provider):
        if provider not in DOMAINS:
            raise ValueError('Furnizor necunoscut.')
        self.provider = provider
        self.path = Path(profile).parent / 'private_sessions' / ('login-' + provider.lower().replace(' ', '-') + '.enc')
        self.cipher = Fernet(key.encode())

    def save(self, username, password, url, *, strict_location=True):
        if not provider_url(self.provider, url) or not username.strip() or not password or max(len(username), len(password)) > 512:
            raise ValueError('Date de autentificare sau domeniu invalid.')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {'provider': self.provider, 'username': username.strip(), 'password': password,
                'login_url': login_identity(url), 'strict_location': bool(strict_location)}
        with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='provider-', delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            output.write(self.cipher.encrypt(json.dumps(data).encode()))
        try:
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self):
        if not self.path.exists():
            return None
        data = json.loads(self.cipher.decrypt(self.path.read_bytes()))
        if data.get('provider') != self.provider or not provider_url(self.provider, data.get('login_url', '')):
            raise ValueError('Identitatea furnizorului nu corespunde.')
        return data


async def upstream_error(page):
    return bool(await page.locator('body').evaluate(r"""body=>{
        const text=body.innerText.trim();
        return text.length<3000 && /^(?:500|502|503|504)\s+(?:Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time.?out)/i.test(text)
    }"""))


async def active_verification(page):
    # Passive reCAPTCHA badges alone do not mean there is a challenge.
    return bool(await page.locator('body').evaluate(r"""body=>{
        const shown=e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
        const token=body.querySelector('[name="g-recaptcha-response"]');
        if(token?.value.trim())return false;
        return [...body.querySelectorAll('iframe')].some(e=>shown(e)&&
            !e.closest('.grecaptcha-badge')&&!/[?&]size=invisible(?:&|$)/i.test(e.src)&&
            /recaptcha.*(?:anchor|bframe)|hcaptcha|challenges.cloudflare.com/i.test(e.src)) ||
            [...body.querySelectorAll('input')].some(e=>shown(e)&&/otp|one-time|verification.?code|captcha/i.test(e.name+' '+e.autocomplete));
    }"""))


async def authenticated(page, provider):
    if not provider_url(provider, page.url) or await upstream_error(page):
        return False
    if await page.locator('input[type=password]:visible').count():
        return False
    # Positive account evidence, never just a tab or the absence of a password.
    if await page.locator('a[href*="logout"],a[href*="logoff"],form[action*="logout"],button[data-action="logout"]').count():
        return True
    if provider == 'AutoTotal':
        # Its account menu can use a JavaScript action instead of an href.
        return bool(await page.locator('a,button,[role=button],[ng-click*="logout"],[onclick*="logout"],a.dropdown-item > span').filter(
            has_text=re.compile(r'^\s*(?:Deconectare|Ie[sșş]ire|Logout|Log out|Sign out)\s*$', re.I)).count())
    return False


async def expired_autototal_form(page):
    if (not provider_url('AutoTotal', page.url)
            or urlparse(page.url).path.rstrip('/') != '/login'):
        return False
    return bool(await page.locator('body').evaluate(r"""body=>{
        const t=body.innerText.trim();
        return t.length<1000 && /An Error Was Encountered/i.test(t) &&
            /The action you have requested is not allowed[.]/i.test(t);
    }"""))


async def reconnect(page, credentials, provider):
    if not credentials or not provider_url(provider, page.url):
        return 'login_required'
    if provider == 'DSM':
        from dsm_session_vault import trusted_login
        if not trusted_login(page.url):
            return 'login_required'
    elif credentials.get('strict_location', True) and login_identity(page.url) != credentials.get('login_url'):
        return 'login_location_changed'
    if await active_verification(page):
        return 'human_verification_required'
    password = page.locator('input[type=password]:visible')
    if await password.count() != 1:
        return 'unsupported_login_form'
    form = password.locator('xpath=ancestor::form')
    if await form.count() != 1:
        return 'unsupported_login_form'
    username = form.locator('input[type=email]:visible,input[type=text]:visible')
    button = form.get_by_role('button', name=re.compile(r'^(?:Autentificare|Autentifică-te|Login|Log in|Sign in|Conectare|Conectează-te|Intră în cont)$', re.I))
    if await username.count() != 1 or await button.count() != 1:
        return 'unsupported_login_form'
    user_value = (await username.input_value()).strip()
    password_value = await password.input_value()
    if (password_value not in {'', credentials['password']}
            or user_value not in {'', credentials['username']}):
        return 'human_login_in_progress'
    # Re-filling unchanged credentials invalidates a freshly completed CAPTCHA
    # on AutoTotal. Submit matching fields without firing more input events.
    fields_changed = user_value != credentials['username'] or password_value != credentials['password']
    if user_value != credentials['username']:
        await username.fill(credentials['username'], timeout=10000)
    if password_value != credentials['password']:
        await password.fill(credentials['password'], timeout=10000)
    if fields_changed and await active_verification(page):
        return 'human_verification_required'
    await button.click(timeout=15000)
    for _ in range(10):
        if await authenticated(page, provider):
            return 'connected'
        if await upstream_error(page):
            return 'upstream_unavailable'
        if await active_verification(page):
            return 'human_verification_required'
        await asyncio.sleep(1)
    if not await page.locator('input[type=password]:visible').count():
        return 'unverified'
    if provider_url(provider, page.url) and await password.count() == 1:
        await password.fill('', timeout=3000)
        await username.fill('', timeout=3000)
    return 'login_failed'


async def session_response_metadata(request, url, *, timeout):
    """Copy only metadata; release the Playwright-owned response body first."""
    response = None
    try:
        response = await request.get(url, timeout=timeout)
        return response.status, response.url, response.ok
    finally:
        if response is not None:
            await response.dispose()


class ProviderSessionKeeper:
    def __init__(self, profile, key=None):
        self.vaults = {p: ProviderLoginVault(profile, key, p) if key else None for p in DOMAINS if p != 'DSM'}
        self.attempts = {}
        self.error_reloads = {}
        self.last_keepalive = {}
        self.last_restore = {}

    def idle(self):
        root = os.getenv('ALBATROS_DATA_DIR')
        if not root:
            return True
        from browser_resources import busy_reason
        return busy_reason(root) is None

    async def keep_autototal(self, page):
        """Refresh the HTTP session, without navigating the catalogue tab."""
        now = time.monotonic()
        if now - self.last_keepalive.get('AutoTotal', -600) < 600:
            return None
        self.last_keepalive['AutoTotal'] = now
        try:
            status, url, ok = await session_response_metadata(page.request, AUTOTOTAL_HOME, timeout=10000)
        except Exception:
            self.last_keepalive['AutoTotal'] = now - 540
            return 'upstream_unavailable'
        if status in {401, 403} or urlparse(url).path.rstrip('/') == '/login':
            return 'login_required'
        return 'ok' if ok else 'upstream_unavailable'

    async def check(self, context):
        result = {}
        for provider, vault in self.vaults.items():
            configured = bool(vault and vault.path.exists())
            try:
                state = {'auto_login_configured': configured, 'status': 'tab_missing', 'checked_at': time.time()}
                if provider == 'Haynes':
                    state['status'] = 'standby'
                    result[provider] = state
                    continue
                pages = [p for p in context.pages if not p.is_closed() and provider_url(provider, p.url)]
                if (provider == 'AutoTotal' and not pages and configured and self.idle()
                        and time.monotonic() - self.last_restore.get(provider, -60) >= 60):
                    self.last_restore[provider] = time.monotonic()
                    page = await context.new_page()
                    pages = [page]
                    try:
                        await page.goto(AUTOTOTAL_HOME, wait_until='domcontentloaded', timeout=15000)
                    except Exception:
                        if not provider_url(provider, page.url):
                            await page.close()
                        raise
                    state['tab_restored'] = True
                connected = False
                for page in pages:
                    if await authenticated(page, provider):
                        connected = True
                        if provider == 'AutoTotal' and self.idle():
                            outcome = await self.keep_autototal(page)
                            if outcome == 'login_required':
                                connected = False
                                # An idle home page may return to a fresh login
                                # with GET. Never reload a submitted POST form.
                                path = urlparse(page.url).path
                                if path == '/' or path.startswith(('/search/', '/auto/vin/', '/uleiuri/')):
                                    await page.goto(AUTOTOTAL_LOGIN, wait_until='domcontentloaded', timeout=15000)
                            elif outcome == 'upstream_unavailable':
                                state['keepalive_status'] = outcome
                        break
                if connected:
                    state['status'] = 'connected'
                    self.attempts.pop(provider, None)
                    result[provider] = state
                    continue
                for page in pages:
                    if provider == 'AutoTotal' and await expired_autototal_form(page):
                        state['status'] = 'login_form_expired'
                        if self.idle() and time.monotonic() - self.error_reloads.get(provider, -60) >= 60:
                            self.error_reloads[provider] = time.monotonic()
                            await page.goto(AUTOTOTAL_LOGIN, wait_until='domcontentloaded', timeout=15000)
                            state['status'] = 'login_form_refreshed'
                        break
                    if await upstream_error(page):
                        state['status'] = 'upstream_unavailable'
                        if time.monotonic() - self.error_reloads.get(provider, 0) > 60:
                            self.error_reloads[provider] = time.monotonic()
                            await page.reload(wait_until='domcontentloaded', timeout=15000)
                        continue
                    if not await page.locator('input[type=password]:visible').count():
                        state['status'] = 'unverified'
                        continue
                    state['status'] = 'login_required'
                    if not configured:
                        continue
                    if not self.idle():
                        state['status'] = 'retry_wait'
                        continue
                    version = vault.path.stat().st_mtime_ns
                    last, failures, previous_version = self.attempts.get(provider, (0, 0, 0))
                    if version != previous_version:
                        last, failures = 0, 0
                    delay = 1800 if failures >= 3 else 60 * 2 ** failures
                    if time.monotonic() - last < delay:
                        state['status'] = 'retry_wait'
                        continue
                    outcome = await reconnect(page, vault.load(), provider)
                    if outcome in {'connected', 'login_failed'}:
                        self.attempts[provider] = (time.monotonic(), failures + (outcome == 'login_failed'), version)
                    state['status'] = outcome
                    break
                result[provider] = state
            except Exception:
                result[provider] = {"auto_login_configured": configured, "status": "browser_unavailable", "checked_at": time.time()}
        return result
