"""Shared, presentation-only navigation styles for authenticated app pages."""
from functools import lru_cache
from pathlib import Path

from starlette.responses import Response, StreamingResponse
import anyio

STYLE_MARKER = b'id="albatros-navigation-style"'
PAGE_ROOTS = frozenset({
    'cereri', 'oferta', 'ofertare', 'tehnician', 'programator', 'conversatie',
    'master', 'baza-dsm', 'arhiva', 'acuratete', 'dispozitive',
})
MAX_HEAD_BYTES = 64 * 1024


def style_prefix(body, path):
    if path.rstrip('/') == '/programator':
        body = body.replace('<a href="/cereri">← Centrala cereri</a>'.encode(),
                            '<a href="/cereri">Înapoi la Centrala cereri</a>'.encode())
    if STYLE_MARKER not in body:
        end = body.lower().find(b'</head>')
        body = (body[:end] + navigation_style() + body[end:]) if end >= 0 else navigation_style() + body
    return body


class StyledStreamingResponse(StreamingResponse):
    def __init__(self, source, path, headers):
        self.source = source
        super().__init__(self.chunks(path), status_code=source.status_code,
                         headers=headers, background=source.background)

    async def chunks(self, path):
        prefix, emitted = bytearray(), False
        async for chunk in self.source.body_iterator:
            chunk = chunk.encode(self.charset) if isinstance(chunk, str) else chunk
            if emitted:
                yield chunk
                continue
            take = min(MAX_HEAD_BYTES - len(prefix), len(chunk))
            prefix.extend(chunk[:take])
            if b'</head>' in prefix.lower() or STYLE_MARKER in prefix or len(prefix) == MAX_HEAD_BYTES:
                yield style_prefix(bytes(prefix), path)
                prefix.clear()
                emitted = True
                if take < len(chunk):
                    yield chunk[take:]
        if not emitted:
            yield style_prefix(bytes(prefix), path)

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    close = getattr(self.source.body_iterator, 'aclose', None)
                    if close:
                        await close()
                finally:
                    close = getattr(self.source, 'close', None)
                    if close:
                        await close()


@lru_cache(maxsize=1)
def navigation_style() -> bytes:
    css = (Path(__file__).parent / 'static' / 'navigation.css').read_bytes()
    return b'<style id="albatros-navigation-style">' + css + b'</style>'


async def with_navigation_style(path: str, response):
    """Leave route behavior, forms, permissions and non-HTML responses intact."""
    if (path.strip('/').split('/')[0] not in PAGE_ROOTS
            or response.status_code != 200
            or 'text/html' not in response.headers.get('content-type', '')
            or response.headers.get('content-encoding')):
        return response
    headers = dict(response.headers)
    for key in ('content-length', 'etag', 'content-md5'):
        headers.pop(key, None)
    body = getattr(response, 'body', None)
    if body is None:
        return StyledStreamingResponse(response, path, headers)
    body = style_prefix(body, path)
    return Response(body, status_code=response.status_code, headers=headers,
                    background=response.background)
