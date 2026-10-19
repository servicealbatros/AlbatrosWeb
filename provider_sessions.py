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
    return bool(await page.locator('a[href*="logout"],a[href*="logoff"],form[action*="logout"],button[data-action="logout"]').count())


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
    if await password.input_value() or (await username.input_value()).strip() not in {'', credentials['username']}:
        return 'human_login_in_progress'
    await username.fill(credentials['username'], timeout=10000)
    await password.fill(credentials['password'], timeout=10000)
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


class ProviderSessionKeeper:
    def __init__(self, profile, key):
        self.vaults = {p: ProviderLoginVault(profile, key, p) for p in DOMAINS if p != 'DSM'}
        self.attempts = {}
        self.error_reloads = {}

    async def check(self, context):
        result = {}
        for provider, vault in self.vaults.items():
            try:
                state = {'auto_login_configured': vault.path.exists(), 'status': 'tab_missing', 'checked_at': time.time()}
                pages = [p for p in context.pages if not p.is_closed() and provider_url(provider, p.url)]
                connected = False
                for page in pages:
                    if await authenticated(page, provider):
                        connected = True
                        break
                if connected:
                    state['status'] = 'connected'
                    self.attempts.pop(provider, None)
                    result[provider] = state
                    continue
                for page in pages:
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
                    if not vault.path.exists():
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
                result[provider] = {"auto_login_configured": vault.path.exists(), "status": "browser_unavailable", "checked_at": time.time()}
        return result
