"""Private encrypted DSM session cookies; never passwords or public exports."""
import json
import os
import time
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from cryptography.fernet import Fernet

HOSTS = {'apps.dsmpro.ro', 'appm.dsmpro.ro', 's.servicemanager.q-service.ro'}

def trusted_login(url):
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.hostname in {'appm.dsmpro.ro', 'apps.dsmpro.ro'}
            and not parsed.username and not parsed.password
            and parsed.path.rstrip('/').endswith('/login'))

class DsmLoginVault:
    """Explicitly configured credentials, encrypted locally, never exported."""
    def __init__(self, profile, key):
        self.path = Path(profile).parent / 'private_sessions' / 'dsm-login.enc'
        self.cipher = Fernet(key.encode())

    def save(self, username, password):
        if not username.strip() or not password or max(len(username), len(password)) > 512:
            raise ValueError('Completează utilizatorul și parola DSM.')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='login-', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            output.write(self.cipher.encrypt(json.dumps({'username': username.strip(), 'password': password}).encode()))
        try:
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self):
        return json.loads(self.cipher.decrypt(self.path.read_bytes())) if self.path.exists() else None

def trusted_landing(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password or '/special/' not in parsed.path:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))

def valid_cookie(cookie, now):
    domain = str(cookie.get('domain', '')).lstrip('.')
    expires = float(cookie.get('expires', -1))
    return (domain in HOSTS or domain == 'dsmpro.ro') and (expires == -1 or expires > now)

class DsmSessionVault:
    def __init__(self, profile, key):
        self.path = Path(profile).parent / 'private_sessions' / 'dsm.enc'
        self.cipher = Fernet(key.encode())

    def save(self, cookies, landing):
        landing = trusted_landing(landing)
        if not landing:
            raise ValueError('Only an authenticated DSM application page can be saved')
        cookies = [c for c in cookies if valid_cookie(c, time.time())]
        if not cookies:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = json.dumps({'cookies': cookies, 'landing': landing, 'saved_at': time.time()}).encode()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix='dsm-', suffix='.tmp', delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                output.write(self.cipher.encrypt(raw))
            for attempt in range(10):
                try:
                    temporary.replace(self.path)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.02)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return True

    def load(self):
        if not self.path.exists():
            return None
        data = json.loads(self.cipher.decrypt(self.path.read_bytes()))
        if not trusted_landing(data.get('landing', '')):
            raise ValueError('Invalid DSM landing')
        data['cookies'] = [c for c in data['cookies'] if valid_cookie(c, time.time())]
        return data
