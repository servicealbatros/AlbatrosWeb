import asyncio
import hashlib
import hmac
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from fastapi import FastAPI, File, UploadFile

from bounded_http import BodyLimitMiddleware, read_limited_body
import reception_bridge as bridge
import navigation_ui as navigation
from security import RateLimiter, SecurityPolicy, Action, SecurityViolation
import provider_test_console as console


def scope(headers=(), method='POST', path='/api/input'):
    return {'type':'http', 'asgi':{'version':'3.0','spec_version':'2.4'}, 'method':method,
            'path':path,'raw_path':path.encode(),'query_string':b'', 'scheme':'https',
            'server':('example.test',443), 'headers':list(headers)}


class BodyTests(unittest.IsolatedAsyncioTestCase):
    async def run_body(self, chunks, headers=()):
        messages, reads = [], []
        source = iter(chunks)
        async def receive():
            item = next(source)
            reads.append(item)
            return {'type':'http.request','body':item,'more_body':True}
        async def app(s, recv, send):
            request = Request(s, recv)
            body = await read_limited_body(request, 8)
            await Response(body)(s, recv, send)
        await BodyLimitMiddleware(app, maximum=8)(scope(headers), receive, messages.append_async if hasattr(messages,'append_async') else self.sender(messages))
        return messages, reads

    def sender(self, messages):
        async def send(item):
            messages.append(item)
        return send

    async def test_declared_oversize_rejected_without_reading(self):
        messages, reads = await self.run_body([], [(b'content-length',b'9')])
        self.assertEqual(messages[0]['status'], 413)
        self.assertFalse(reads)

    async def test_actual_chunk_limits_with_missing_or_false_content_length(self):
        for headers in ([], [(b'content-length',b'1')]):
            messages, reads = await self.run_body([b'1234',b'5678',b'9',b'must-not-be-read'], headers)
            self.assertEqual(messages[0]['status'], 413)
            self.assertEqual(len(reads), 3)

    async def test_valid_bytes_are_preserved_and_reusable_for_hmac(self):
        chunks = iter([b'{"x":', b'1}', b''])
        async def receive():
            value = next(chunks)
            return {'type':'http.request','body':value,'more_body':bool(value)}
        request = Request(scope(), receive)
        raw = await read_limited_body(request, 8)
        self.assertEqual(await request.body(), b'{"x":1}')
        secret = b'test-key'
        self.assertEqual(hmac.new(secret,raw,hashlib.sha256).digest(),
                         hmac.new(secret,b'{"x":1}',hashlib.sha256).digest())

    async def test_upload_reads_only_maximum_plus_one(self):
        import app as core
        maximum = 12 * 1024 * 1024
        upload = UploadFile(io.BytesIO(b'x'*(maximum+4096)), filename='image.jpg', headers=Headers({'content-type':'image/jpeg'}))
        upload.read = AsyncMock(wraps=upload.read)
        try:
            with self.assertRaises(core.HTTPException) as error, patch.object(core,'read_registration_document') as vision:
                await core.api_talon(upload, 'test-case')
            self.assertEqual(error.exception.status_code,400)
            upload.read.assert_awaited_once_with(maximum+1)
            vision.assert_not_called()
        finally:
            await upload.close()

    async def test_valid_upload_keeps_vision_and_chat_behavior(self):
        import app as core
        upload = UploadFile(io.BytesIO(b'fixture'), filename='image.png', headers=Headers({'content-type':'image/png'}))
        try:
            with patch.object(core,'read_registration_document',return_value={'make':'Fixture'}), \
                    patch.object(core,'chat',AsyncMock(return_value='answer')):
                response = await core.api_talon(upload,'test-case')
            self.assertEqual(response['answer'],'answer')
            self.assertEqual(response['extracted']['make'],'Fixture')
        finally:
            await upload.close()

    async def test_multipart_without_content_length_closes_partial_upload_on_limit(self):
        import starlette.formparsers as parser
        application = FastAPI()
        @application.post('/upload')
        async def upload(file: UploadFile = File(...)):
            return {'size':len(await file.read(8193))}
        application.add_middleware(BodyLimitMiddleware, maximum=8192)
        files, yielded = [], []
        original = parser.SpooledTemporaryFile
        def create(*args, **kwargs):
            file = original(*args, **kwargs)
            files.append(file)
            return file
        async def body():
            yield b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="f.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
            for _ in range(100):
                yielded.append(1)
                yield b'x'*1024
            yield b'\r\n--fixture--\r\n'
        with patch.object(parser,'SpooledTemporaryFile',side_effect=create):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url='https://test') as client:
                response = await client.post('/upload',content=body(),headers={'content-type':'multipart/form-data; boundary=fixture'})
        self.assertEqual(response.status_code,413)
        self.assertLess(len(yielded),10)
        self.assertTrue(files)
        self.assertTrue(all(file.closed for file in files))

    async def test_cloud_webhook_limits_preserve_hmac_and_authentication(self):
        import cloud_app
        raw = b'{"entry":[]}'
        signature = 'sha256='+hmac.new(b'fixture-secret',raw,hashlib.sha256).hexdigest()
        async def forwarded(request, actor):
            self.assertEqual(actor,'meta-webhook')
            self.assertEqual(await request.body(),raw)
            return Response('accepted')
        gateway = cloud_app.CloudGateway(AsyncMock())
        with patch.dict(os.environ,{'WHATSAPP_APP_SECRET':'fixture-secret'}), \
                patch.object(cloud_app,'bridge_url',return_value='https://worker.test'), \
                patch.object(cloud_app,'forward',AsyncMock(side_effect=forwarded)) as forward:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway),base_url='https://test') as client:
                valid = await client.post('/webhook/whatsapp',content=raw,headers={'x-hub-signature-256':signature})
                invalid = await client.post('/webhook/whatsapp',content=raw,headers={'x-hub-signature-256':'sha256=bad'})
                oversized = await client.post('/webhook/whatsapp',content=b'',headers={'content-length':str(21*1024*1024)})
                async def chunks():
                    for _ in range(322):
                        yield b'x'*65536
                missing_length = await client.post('/webhook/whatsapp',content=chunks())
        self.assertEqual([valid.status_code,invalid.status_code,oversized.status_code,missing_length.status_code], [200,401,413,413])
        forward.assert_awaited_once()


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_releases_connection_on_upstream_failure_and_failed_headers(self):
        from starlette.requests import ClientDisconnect
        for failed_headers in (False,True):
            class FailedStream(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'x'*65536
                    raise httpx.ReadError('upstream disconnected')
                async def aclose(self):
                    pass
            upstream=httpx.Response(200,stream=FailedStream())
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:upstream))
            client.aclose=AsyncMock(wraps=client.aclose)
            async def receive():
                return {'type':'http.request','body':b'', 'more_body':False}
            with patch.dict(os.environ,{'RECEPTION_BRIDGE_URL':'https://worker.test','RECEPTION_BRIDGE_SECRET':'fixture-secret-'*4}), \
                    patch.object(bridge.httpx,'AsyncClient',return_value=client):
                response=await bridge.forward(Request(scope(method='GET'),receive),'counselor')
            if failed_headers:
                async def send(_):
                    raise OSError('downstream disconnected')
                with self.assertRaises(ClientDisconnect):
                    await response(scope(method='GET'),receive,send)
            else:
                with self.assertRaises(httpx.ReadError):
                    async for _ in response.body_iterator:
                        pass
            self.assertTrue(upstream.is_closed)
            client.aclose.assert_awaited_once()

    async def test_bridge_streams_without_reading_entire_response_and_closes(self):
        sent = []
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(100):
                    sent.append(1)
                    yield b'x'*65536
            async def aclose(self):
                sent.append('closed')
        upstream = httpx.Response(200, stream=Stream(), headers={'content-type':'application/octet-stream'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _:upstream))
        original = client.aclose
        client.aclose = AsyncMock(wraps=original)
        request = Request(scope(method='GET',path='/api/requests/1/document'))
        async def receive():
            return {'type':'http.request','body':b'', 'more_body':False}
        request._receive = receive
        with patch.dict(os.environ,{'RECEPTION_BRIDGE_URL':'https://worker.test','RECEPTION_BRIDGE_SECRET':'fixture-secret-'*4}), \
                patch.object(bridge.httpx,'AsyncClient',return_value=client):
            result = await bridge.forward(request,'counselor')
        self.assertFalse(sent)
        first = await anext(result.body_iterator)
        self.assertEqual(len(first),65536)
        self.assertLess(len(sent),100)
        await result.body_iterator.aclose()
        self.assertIn('closed',sent)
        self.assertTrue(upstream.is_closed)
        client.aclose.assert_awaited_once()

    async def test_html_styling_is_bounded_and_releases_proxy_on_disconnect(self):
        read, closed = [], []
        async def chunks():
            try:
                for _ in range(100):
                    read.append(1)
                    yield b'x'*65536
            finally:
                closed.append(1)
        original = StreamingResponse(chunks(),media_type='text/html')
        styled = await navigation.with_navigation_style('/cereri',original)
        self.assertFalse(read)
        first = await anext(styled.body_iterator)
        self.assertIn(navigation.STYLE_MARKER,first)
        self.assertEqual(len(read),1)
        async def broken_send(_):
            raise OSError('client disconnected')
        from starlette.requests import ClientDisconnect
        with self.assertRaises(ClientDisconnect):
            await styled(scope(method='GET'), AsyncMock(), broken_send)
        self.assertEqual(closed,[1])


class CacheTests(unittest.TestCase):
    def test_rate_limiter_is_bounded_and_does_not_evict_active_clients(self):
        limiter = RateLimiter(max_keys=2)
        with patch('security.time.monotonic',return_value=100):
            self.assertTrue(limiter.allow('a',1,10))
            self.assertTrue(limiter.allow('b',1,10))
            for i in range(50):
                self.assertFalse(limiter.allow(str(i),1,10))
            self.assertFalse(limiter.allow('a',1,10))
        self.assertEqual(len(limiter._events),2)
        with patch('security.time.monotonic',return_value=111):
            limiter.cleanup()
            self.assertFalse(limiter._events)
            self.assertTrue(limiter.allow('new',1,10))

    def test_confirmation_tokens_expire_and_are_consumed_atomically(self):
        from concurrent.futures import ThreadPoolExecutor
        with TemporaryDirectory() as root:
            policy = SecurityPolicy(Path(root))
            token = policy.create_confirmation(Action.MODIFY_BOOKING,'1','fixture')
            def use(_):
                try:
                    policy.authorize(Action.MODIFY_BOOKING, resource_id='1', confirmation_token=token)
                    return True
                except SecurityViolation:
                    return False
            with ThreadPoolExecutor(8) as executor:
                self.assertEqual(sum(executor.map(use,range(20))),1)
            self.assertFalse(policy._confirmations)
            with patch('security.time.time',return_value=10):
                policy.create_confirmation(Action.MODIFY_BOOKING,'2','fixture',ttl_seconds=1)
            with patch('security.time.time',return_value=12):
                policy.cleanup_expired()
            self.assertFalse(policy._confirmations)
            with patch('security.time.time',return_value=100):
                token=policy.create_confirmation(Action.MODIFY_BOOKING,'3','fixture',ttl_seconds=7200)
            with patch('security.time.time',return_value=4000):
                policy.authorize(Action.MODIFY_BOOKING,resource_id='3',confirmation_token=token)

    def test_confirmation_capacity_keeps_existing_authorizations_and_rate_limit_is_thread_safe(self):
        from concurrent.futures import ThreadPoolExecutor
        with TemporaryDirectory() as root:
            policy=SecurityPolicy(Path(root));policy.max_confirmations=1
            token=policy.create_confirmation(Action.MODIFY_BOOKING,'1','fixture')
            with self.assertRaises(SecurityViolation):
                policy.create_confirmation(Action.MODIFY_BOOKING,'2','fixture')
            policy.authorize(Action.MODIFY_BOOKING,resource_id='1',confirmation_token=token)
            self.assertTrue(policy.create_confirmation(Action.MODIFY_BOOKING,'2','fixture'))
        limiter=RateLimiter()
        with ThreadPoolExecutor(8) as executor:
            self.assertEqual(sum(executor.map(lambda _:limiter.allow('same',5,60),range(100))),5)

    def test_console_expiry_and_capacity_do_not_discard_active_sessions(self):
        with patch.dict(console.GRANTS, {'old':1,'valid':100},clear=True), \
                patch.dict(console.SESSIONS,{'expired':2,'active':100},clear=True):
            console.cleanup_access_tokens(now=10)
            self.assertEqual(console.GRANTS,{'valid':100})
            self.assertEqual(console.SESSIONS,{'active':100})

    def test_console_capacity_fails_closed_instead_of_evicting_sessions(self):
        async def run():
            request = SimpleNamespace(headers={'origin':'https://test','host':'test'}, cookies={})
            with patch.dict(console.GRANTS,{'grant':999},clear=True), \
                    patch.dict(console.SESSIONS,{'active':999},clear=True), \
                    patch.object(console,'MAX_SESSIONS',1), patch.object(console.time,'time',return_value=10), \
                    patch.object(console,'enabled'):
                with self.assertRaises(console.HTTPException) as error:
                    await console.unlock(console.Unlock(token='grant'),request)
                self.assertEqual(error.exception.status_code,503)
                self.assertIn('active',console.SESSIONS)
                self.assertIn('grant',console.GRANTS)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
