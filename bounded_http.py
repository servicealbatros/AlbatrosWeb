"""Byte limits applied before buffering or multipart parsing (not just headers)."""
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

MAX_BODY = 20 * 1024 * 1024
MAX_BACKUP_BODY = 256 * 1024 * 1024


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, 'Payload too large')


def check_length(headers, maximum):
    values = headers.getlist('content-length')
    if not values:
        return
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        raise HTTPException(400, 'Invalid Content-Length')
    declared = values[0].lstrip('0') or '0'
    if len(declared) > len(str(maximum)) or int(declared) > maximum:
        raise BodyTooLarge()


async def read_limited_body(request, maximum=MAX_BODY):
    check_length(request.headers, maximum)
    cached = getattr(request, '_body', None)
    if cached is not None:
        if len(cached) > maximum:
            raise BodyTooLarge()
        return cached
    result = bytearray()
    async for chunk in request.stream():
        if len(result) + len(chunk) > maximum:
            raise BodyTooLarge()
        result.extend(chunk)
    # Starlette's middleware uses this cache to replay exactly the signed bytes.
    request._body = bytes(result)
    return request._body


class BodyLimitMiddleware:
    def __init__(self, app, maximum=MAX_BODY):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        from starlette.datastructures import Headers
        maximum = MAX_BACKUP_BODY if scope.get('path') == '/api/admin/worker-backup' else self.maximum
        count, started = 0, False

        async def bounded_receive():
            nonlocal count
            message = await receive()
            if message['type'] == 'http.request':
                count += len(message.get('body', b''))
                if count > maximum:
                    raise BodyTooLarge()
            return message

        async def tracked_send(message):
            nonlocal started
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            check_length(Headers(scope=scope), maximum)
            return await self.app(scope, bounded_receive, tracked_send)
        except HTTPException as error:
            if started:
                raise
            return await JSONResponse({'detail': error.detail}, status_code=error.status_code)(scope, receive, send)
