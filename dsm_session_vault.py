"""Private encrypted DSM session cookies; never passwords or public exports."""
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from cryptography.fernet import Fernet

HOSTS = {'apps.dsmpro.ro', 'appm.dsmpro.ro', 's.servicemanager.q-service.ro'}

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
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('wb') as output:
            os.chmod(temporary, 0o600)
            output.write(self.cipher.encrypt(raw))
        temporary.replace(self.path)
        return True

    def load(self):
        if not self.path.exists():
            return None
        data = json.loads(self.cipher.decrypt(self.path.read_bytes()))
        if not trusted_landing(data.get('landing', '')):
            raise ValueError('Invalid DSM landing')
        data['cookies'] = [c for c in data['cookies'] if valid_cookie(c, time.time())]
        return data
