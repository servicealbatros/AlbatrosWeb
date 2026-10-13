"""Authenticated connection between the cloud reception and its owned worker.

Dashboard identities do not receive an administrator token. Requests are signed
including method, target and body, and expire quickly. No supplier cookies leave
the worker computer.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import urlparse

import httpx
from starlette.responses import JSONResponse, Response

_used = {}
_lock = threading.Lock()
MAX_BODY = 20 * 1024 * 1024


def dashboard_route(path):
    if path.startswith('/api/dashboard/devices') or path == '/api/dashboard/access-check':
        return False
    return path.startswith(('/cereri', '/conversatie/', '/oferta/', '/ofertare/',
        '/tehnician/', '/programator', '/acuratete', '/arhiva', '/baza-dsm',
        '/api/requests', '/api/scheduler/', '/api/dashboard/', '/api/dsm/', '/static/'))


def signature(secret, stamp, nonce, actor, method, target, body):
    message = json.dumps([stamp, nonce, actor, method, target,
                         hashlib.sha256(body).hexdigest()], separators=(',', ':')).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_request(headers, method, target, body):
    secret = os.getenv('RECEPTION_BRIDGE_SECRET', '')
    if not secret or len(secret) < 32:
        raise ValueError('bridge_disabled')
    actor = headers.get('x-albatros-actor', '')
    stamp = headers.get('x-albatros-time', '')
    nonce = headers.get('x-albatros-nonce', '')
    if not actor or len(actor) > 120 or not nonce or len(nonce) > 100:
        raise ValueError('invalid_bridge_identity')
    if abs(time.time() - int(stamp)) > 90:
        raise ValueError('expired_bridge_request')
    expected = signature(secret, stamp, nonce, actor, method, target, body)
    if not hmac.compare_digest(expected, headers.get('x-albatros-signature', '')):
        raise ValueError('invalid_bridge_signature')
    with _lock:
        now = time.time()
        for key in list(_used):
            if _used[key] < now - 100:
                del _used[key]
        if nonce in _used:
            raise ValueError('replayed_bridge_request')
        _used[nonce] = now
    return actor


def bridge_url():
    value = os.getenv('RECEPTION_BRIDGE_URL', '').rstrip('/')
    if not value:
        return ''
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Bridge must be an HTTPS origin')
    return value


async def forward(request, actor):
    try:
        origin = bridge_url()
        secret = os.getenv('RECEPTION_BRIDGE_SECRET', '')
        if not origin or len(secret) < 32:
            raise ValueError('Bridge is not configured')
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({'detail': 'Fișierul depășește limita de 20 MB.'}, status_code=413)
        target = request.url.path + ('?' + request.url.query if request.url.query else '')
        stamp, nonce = str(int(time.time())), secrets.token_hex(20)
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() in {'accept', 'content-type', 'x-human-action',
                                      'x-hub-signature-256', 'range'}}
        headers.update({'x-albatros-actor': actor, 'x-albatros-time': stamp,
            'x-albatros-nonce': nonce,
            'x-albatros-signature': signature(secret, stamp, nonce, actor, request.method, target, body),
            'ngrok-skip-browser-warning': 'true'})
        async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), follow_redirects=False) as client:
            result = await client.request(request.method, origin + target, headers=headers, content=body)
        response_headers = {key: value for key, value in result.headers.items()
                            if key.lower() in {'content-type', 'content-disposition', 'location', 'accept-ranges', 'content-range'}}
        location = response_headers.get('location', '')
        if location.startswith(origin + '/'):
            response_headers['location'] = location[len(origin):]
        response_headers.update({'cache-control': 'no-store', 'x-content-type-options': 'nosniff',
                                 'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer'})
        return Response(result.content, result.status_code, headers=response_headers)
    except (httpx.HTTPError, ValueError):
        # A failed worker call is never retried here: mutations may have succeeded.
        return JSONResponse({'detail': 'Legătura cu robotul nu este disponibilă. Verifică laptopul principal.'}, status_code=503)
