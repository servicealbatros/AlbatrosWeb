"""Shared, presentation-only navigation styles for authenticated app pages."""
from functools import lru_cache
from pathlib import Path

from starlette.responses import Response

STYLE_MARKER = b'id="albatros-navigation-style"'
PAGE_ROOTS = frozenset({
    'cereri', 'oferta', 'ofertare', 'tehnician', 'programator', 'conversatie',
    'master', 'baza-dsm', 'arhiva', 'acuratete', 'dispozitive',
})


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
    body = getattr(response, 'body', None)
    if body is None:
        body = b''.join([chunk async for chunk in response.body_iterator])
    if path.rstrip('/') == '/programator':
        # Older workers still render a text arrow; the shared style supplies it.
        body = body.replace(
            '<a href="/cereri">← Centrala cereri</a>'.encode(),
            '<a href="/cereri">Înapoi la Centrala cereri</a>'.encode(),
        )
    if STYLE_MARKER not in body:
        closing_head = body.lower().find(b'</head>')
        if closing_head >= 0:
            body = body[:closing_head] + navigation_style() + body[closing_head:]
        else:
            body = navigation_style() + body
    headers = dict(response.headers)
    for key in ('content-length', 'etag', 'content-md5'):
        headers.pop(key, None)
    return Response(body, status_code=response.status_code, headers=headers,
                    background=response.background)
