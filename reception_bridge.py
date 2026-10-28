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
import anyio
from starlette.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from bounded_http import MAX_BODY, read_limited_body

_used = {}
_lock = threading.Lock()


def dashboard_route(path):
    if path.startswith('/api/dashboard/devices') or path == '/api/dashboard/access-check':
        return False
    return path == '/master' or path.startswith(('/cereri', '/conversatie/', '/oferta/', '/ofertare/',
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
        if len(_used) >= 10000:
            raise ValueError('bridge_replay_cache_full')
        _used[nonce] = now
    return actor


def bridge_url():
    value = os.getenv('RECEPTION_BRIDGE_URL', '').rstrip('/')
    if not value:
        return ''
    parsed = urlparse(value)
    if os.getenv('ALBATROS_INTERNAL_BRIDGE') == 'true' and value == 'http://127.0.0.1:8010':
        return value
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Bridge must be an HTTPS origin')
    return value


async def forward(request, actor):
    client = result = None
    transferred = False
    try:
        origin = bridge_url()
        secret = os.getenv('RECEPTION_BRIDGE_SECRET', '')
        if not origin or len(secret) < 32:
            raise ValueError('Bridge is not configured')
        body = await read_limited_body(request, MAX_BODY)
        target = request.url.path + ('?' + request.url.query if request.url.query else '')
        stamp, nonce = str(int(time.time())), secrets.token_hex(20)
        headers = {key: value for key, value in request.headers.items()
                   if key.lower() in {'accept', 'content-type', 'x-human-action',
                                      'x-hub-signature-256', 'range'}}
        headers.update({'x-albatros-actor': actor, 'x-albatros-time': stamp,
            'x-albatros-nonce': nonce,
            'x-albatros-signature': signature(secret, stamp, nonce, actor, request.method, target, body),
            'ngrok-skip-browser-warning': 'true'})
        client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10), follow_redirects=False)
        outbound = client.build_request(request.method, origin + target, headers=headers, content=body)
        result = await client.send(outbound, stream=True)
        response_headers = {key: value for key, value in result.headers.items()
                            if key.lower() in {'content-type', 'content-disposition', 'location', 'accept-ranges',
                                               'content-range', 'content-length', 'content-encoding'}}
        location = response_headers.get('location', '')
        if location.startswith(origin + '/'):
            response_headers['location'] = location[len(origin):]
        response_headers.update({'cache-control': 'no-store', 'x-content-type-options': 'nosniff',
                                 'x-frame-options': 'DENY', 'referrer-policy': 'no-referrer'})
        closed = False

        async def close():
            nonlocal closed
            if closed:
                return
            closed = True
            with anyio.CancelScope(shield=True):
                try:
                    await result.aclose()
                finally:
                    await client.aclose()

        async def chunks():
            try:
                async for chunk in result.aiter_raw(chunk_size=64 * 1024):
                    yield chunk
            finally:
                await close()

        reply = OwnedBridgeResponse(chunks(), result.status_code, headers=response_headers,
                                    background=BackgroundTask(close), close=close)
        transferred = True
        return reply
    except (httpx.HTTPError, ValueError):
        # A failed worker call is never retried here: mutations may have succeeded.
        return JSONResponse({'detail': 'Legătura cu robotul nu este disponibilă. Verifică laptopul principal.'}, status_code=503)
    finally:
        if client is not None and not transferred:
            with anyio.CancelScope(shield=True):
                try:
                    if result is not None:
                        await result.aclose()
                finally:
                    await client.aclose()


class OwnedBridgeResponse(StreamingResponse):
    def __init__(self, *args, close, **kwargs):
        super().__init__(*args, **kwargs)
        self.close = close

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        finally:
            await self.close()
