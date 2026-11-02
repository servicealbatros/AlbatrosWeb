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
from playwright.async_api import Error as BrowserError

DOMAINS = {'DSM': ('dsmpro.ro', 'servicemanager.q-service.ro'),
           'Inter Cars': ('intercars.eu',), 'AutoTotal': ('eoriginal.ro',),
           'Haynes': ('workshopdata.com',)}
AUTOTOTAL_HOME = 'https://www3.eoriginal.ro/'
AUTOTOTAL_LOGIN = 'https://www3.eoriginal.ro/login'
INTERCARS_HOME = 'https://ro.e-cat.intercars.eu/ro/'
SUPPLIER_HOMES = {'AutoTotal': AUTOTOTAL_HOME, 'Inter Cars': INTERCARS_HOME}
KEEPALIVE_INTERVAL = 300


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


async def read_session_status(page, url, provider):
    """Read a trusted page using existing cookies, releasing its response body."""
    if not provider_url(provider, url):
        raise ValueError('Untrusted session check')
    response = await page.request.get(url, timeout=10000)
    try:
        destination = urlparse(response.url)
        if (response.status in {401, 403} or not provider_url(provider, response.url)
                or re.search(r'/(?:login|signin|sign-in)(?:\.[a-z]+)?/?$', destination.path, re.I)):
            return 'login_required'
        return 'ok' if response.ok else 'upstream_unavailable'
    finally:
        # APIRequestContext retains bodies until explicit disposal, even if
        # metadata inspection fails. Release before any caller navigates.
        await response.dispose()


async def intercars_email_form(page):
    """Only the pinned Inter Cars identity host may receive a saved email."""
    location = urlparse(page.url)
    if (location.scheme != 'https' or location.hostname != 'account.intercars.eu' or location.port not in (None,443)
            or location.username or location.password
            or not location.path.startswith('/authenticationendpoint/')):
        return None
    username = page.locator('form input[type=email]:visible,form input[type=text]:visible')
    button = page.get_by_role('button', name=re.compile(r'^\s*(?:Continue|Continuă|Continua|Next)\s*$', re.I))
    if await username.count() == 1 and await button.count() == 1:
        identity = await username.evaluate(r"""e=>{
          const hint=[e.name,e.id,e.placeholder,e.autocomplete,e.labels?.[0]?.innerText].join(' ');
          return !/otp|code|cod\b|token|captcha|one.?time|verific/i.test(hint)
            && (e.type==='email'||/email|username|utilizator/i.test(hint));
        }""")
        if identity:
            return username, button
    return None


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
    email_submitted = False
    if provider == 'Inter Cars' and await password.count() == 0:
        first = await intercars_email_form(page)
        if first is None:
            return 'unsupported_login_form'
        username, next_button = first
        existing = (await username.input_value()).strip()
        if existing not in {'', credentials['username']}:
            return 'human_login_in_progress'
        if existing != credentials['username']:
            await username.fill(credentials['username'], timeout=10000)
        if await active_verification(page):
            return 'human_verification_required'
        await next_button.click(timeout=15000)
        for _ in range(10):
            try:
                if not provider_url(provider, page.url):
                    return 'login_location_changed'
                if await active_verification(page):
                    return 'human_verification_required'
                if await authenticated(page, provider):
                    return 'connected'
                if await password.count() == 1:
                    location = urlparse(page.url)
                    if (location.hostname != 'account.intercars.eu'
                            or not location.path.startswith('/authenticationendpoint/')):
                        return 'login_location_changed'
                    email_submitted = True
                    break
            except BrowserError:
                pass  # The email step replaces the document; retry the new DOM.
            await asyncio.sleep(1)
        if not email_submitted:
            return 'login_failed'
    if await password.count() != 1:
        return 'unsupported_login_form'
    form = password.locator('xpath=ancestor::form')
    if await form.count() != 1:
        return 'unsupported_login_form'
    username = form.locator('input[type=email]:visible,input[type=text]:visible')
    username_count = await username.count()
    if provider == 'Inter Cars' and not email_submitted and username_count == 0:
        # Resume after navigation/restart only when the pinned form itself
        # proves which username was submitted. Never infer it from a URL.
        location = urlparse(page.url)
        if (location.hostname == 'account.intercars.eu'
                and location.path.startswith('/authenticationendpoint/')
                and await form.locator('input').evaluate_all(r"""(nodes,expected)=>nodes.some(e=>{
                  const hint=e.name+' '+e.id;
                  const shown=!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
                  return !shown && e.type!=='password' && /user|email|login/i.test(hint)
                    && !/token|csrf|session|code|password/i.test(hint) && e.value.trim()===expected;
                })""", credentials['username'])):
            email_submitted = True
    if username_count != 1 and not (email_submitted and username_count == 0):
        return 'unsupported_login_form'
    names = r'Autentificare|Autentifică-te|Login|Log in|Sign in|Conectare|Conectează-te|Intră în cont'
    if email_submitted:
        names += r'|Continue|Continuă|Continua'
    button = form.get_by_role('button', name=re.compile('^(?:' + names + ')$', re.I))
    if await button.count() != 1:
        return 'unsupported_login_form'
    user_value = (await username.input_value()).strip() if username_count else credentials['username']
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
        try:
            if await authenticated(page, provider):
                return 'connected'
            if await upstream_error(page):
                return 'upstream_unavailable'
            if await active_verification(page):
                return 'human_verification_required'
        except BrowserError:
            pass
        await asyncio.sleep(1)
    if not await page.locator('input[type=password]:visible').count():
        return 'unverified'
    if provider_url(provider, page.url) and await password.count() == 1:
        await password.fill('', timeout=3000)
        await username.fill('', timeout=3000)
    return 'login_failed'


class ProviderSessionKeeper:
    def __init__(self, profile, key):
        self.vaults = {p: ProviderLoginVault(profile, key, p) for p in DOMAINS if p != 'DSM'}
        self.attempts = {}
        self.error_reloads = {}
        self.last_keepalive = {}
        self.keepalive_health = {}
        self.last_restore = {}

    def idle(self, *, allow_queued=False):
        root = os.getenv('ALBATROS_DATA_DIR')
        if not root:
            return True
        from browser_resources import busy_reason
        marker = Path(root) / '.browser-console-activity'
        if marker.exists() and time.time() - marker.stat().st_mtime < 120:
            return False
        return busy_reason(root, include_queued=not allow_queued) is None

    async def keep_session(self, page, provider):
        """Keep the existing supplier HTTP session warm without tab navigation."""
        now = time.monotonic()
        if now - self.last_keepalive.get(provider, -KEEPALIVE_INTERVAL) < KEEPALIVE_INTERVAL:
            return None
        self.last_keepalive[provider] = now
        try:
            outcome = await read_session_status(page, SUPPLIER_HOMES[provider], provider)
            if provider == 'Inter Cars' and outcome == 'login_required' and await authenticated(page, provider):
                # The e-cat root can run a browser-only SSO handshake. A plain
                # APIRequestContext GET cannot complete that JavaScript flow;
                # do not reload a positively authenticated catalogue because of it.
                outcome = 'browser_session_active'
        except Exception:
            self.last_keepalive[provider] = now - KEEPALIVE_INTERVAL + 60
            outcome = 'upstream_unavailable'
        self.keepalive_health[provider] = {'keepalive_status': outcome, 'keepalive_checked_at': time.time()}
        return outcome

    async def keep_autototal(self, page):
        return await self.keep_session(page, 'AutoTotal')

    async def check(self, context):
        result = {}
        for provider, vault in self.vaults.items():
            try:
                state = {'auto_login_configured': vault.path.exists(), 'status': 'tab_missing', 'checked_at': time.time()}
                state.update(self.keepalive_health.get(provider, {}))
                if provider == 'Haynes':
                    state['status'] = 'standby'
                    result[provider] = state
                    continue
                pages = [p for p in context.pages if not p.is_closed() and provider_url(provider, p.url)]
                if (provider in SUPPLIER_HOMES and not pages and vault.path.exists()
                        and self.idle(allow_queued=(provider == 'Inter Cars'))
                        and time.monotonic() - self.last_restore.get(provider, -60) >= 60):
                    self.last_restore[provider] = time.monotonic()
                    page = await context.new_page()
                    pages = [page]
                    try:
                        await page.goto(SUPPLIER_HOMES[provider], wait_until='domcontentloaded', timeout=15000)
                    except Exception:
                        if not provider_url(provider, page.url):
                            await page.close()
                        raise
                    state['tab_restored'] = True
                connected = False
                for page in pages:
                    if await authenticated(page, provider):
                        connected = True
                        if provider in SUPPLIER_HOMES and self.idle():
                            outcome = await self.keep_session(page, provider)
                            state.update(self.keepalive_health.get(provider, {}))
                            if outcome == 'login_required':
                                connected = False
                                # An idle home page may return to a fresh login
                                # with GET. Never reload a submitted POST form.
                                path = urlparse(page.url).path
                                if provider == 'AutoTotal' and (path == '/' or path.startswith(('/search/', '/auto/vin/', '/uleiuri/'))):
                                    await page.goto(AUTOTOTAL_LOGIN, wait_until='domcontentloaded', timeout=15000)
                                elif provider == 'Inter Cars' and path in {'/', '/ro/'}:
                                    await page.goto(INTERCARS_HOME, wait_until='domcontentloaded', timeout=15000)
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
                        if (self.idle(allow_queued=(provider == 'Inter Cars'))
                                and time.monotonic() - self.error_reloads.get(provider, 0) > 60):
                            self.error_reloads[provider] = time.monotonic()
                            await page.reload(wait_until='domcontentloaded', timeout=15000)
                        continue
                    if (not await page.locator('input[type=password]:visible').count()
                            and not (provider == 'Inter Cars' and await intercars_email_form(page))):
                        state['status'] = 'unverified'
                        continue
                    state['status'] = 'login_required'
                    if not vault.path.exists():
                        continue
                    # A queued Inter Cars job cannot be claimed while its tab
                    # is on the login host. Allow that login to break the cycle;
                    # active work, manual login and CAPTCHA remain protected.
                    if not self.idle(allow_queued=(provider == 'Inter Cars')):
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
                    if outcome == 'connected':
                        self.last_keepalive.pop(provider, None)
                    if outcome in {'connected', 'login_failed'}:
                        self.attempts[provider] = (time.monotonic(), failures + (outcome == 'login_failed'), version)
                    state['status'] = outcome
                    break
                result[provider] = state
            except Exception as error:
                result[provider] = {"auto_login_configured": vault.path.exists(), "status": "browser_unavailable",
                                    "error_type": type(error).__name__, "checked_at": time.time()}
        return result
