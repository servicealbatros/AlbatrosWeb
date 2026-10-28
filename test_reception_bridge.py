import asyncio
import os
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from starlette.requests import Request

from reception_bridge import dashboard_route, forward, signature, verify_request


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'RECEPTION_BRIDGE_SECRET': 'test-secret-' * 6,
            'RECEPTION_BRIDGE_URL': 'https://worker.example.com'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def headers(self, nonce, body=b'{}', stamp=None, target='/api/requests/1/offer-start'):
        stamp = str(stamp or int(time.time()))
        h = {'x-albatros-time': stamp, 'x-albatros-nonce': nonce, 'x-albatros-actor': 'counselor'}
        h['x-albatros-signature'] = signature(os.environ['RECEPTION_BRIDGE_SECRET'], stamp,
            nonce, 'counselor', 'POST', target, body)
        return h

    def test_body_target_and_expiry_are_bound_to_signature(self):
        for body, target, stamp in [(b'changed', '/api/requests/1/offer-start', None),
            (b'{}', '/api/requests/2/offer-start', None),
            (b'{}', '/api/requests/1/offer-start', int(time.time()) - 120)]:
            with self.subTest(body=body, target=target, stamp=stamp), self.assertRaises(ValueError):
                verify_request(self.headers('tamper', stamp=stamp), 'POST', target, body)

    def test_replay_is_rejected(self):
        h = self.headers('unique-replay-test')
        self.assertEqual(verify_request(h, 'POST', '/api/requests/1/offer-start', b'{}'), 'counselor')
        with self.assertRaises(ValueError):
            verify_request(h, 'POST', '/api/requests/1/offer-start', b'{}')

    def test_cloud_device_management_and_robot_admin_are_not_forwarded(self):
        for path in ['/admin/robot/enable', '/api/admin/confirmations', '/dispozitive',
                     '/api/dashboard/devices/revoke-all', '/api/dashboard/access-check', '/api/robot/jobs']:
            self.assertFalse(dashboard_route(path))
        self.assertTrue(dashboard_route('/api/scheduler/data'))

    def test_proxy_removes_credentials_and_does_not_retry_failed_mutations(self):
        async def run():
            scope = {'type': 'http', 'method': 'POST', 'path': '/api/scheduler/rebalance',
                'query_string': b'', 'scheme': 'https', 'server': ('app.example.com',443),
                'headers': [(b'authorization', b'Bearer should-not-leave'),
                            (b'cookie',b'private-session'), (b'content-type',b'application/json')]}
            async def receive():
                return {'type':'http.request','body':b'{}','more_body':False}
            client = AsyncMock()
            client.build_request = Mock(return_value=object())
            client.send.side_effect = httpx.ConnectError('unavailable')
            with patch('reception_bridge.httpx.AsyncClient', return_value=client):
                response = await forward(Request(scope, receive), 'counselor')
            self.assertEqual(response.status_code,503)
            self.assertEqual(client.send.call_count,1)
            client.aclose.assert_awaited_once()
            headers = client.build_request.call_args.kwargs['headers']
            self.assertNotIn('authorization', headers)
            self.assertNotIn('cookie', headers)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
