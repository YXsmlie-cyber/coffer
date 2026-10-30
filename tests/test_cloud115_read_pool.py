import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend.cloud115 import Cloud115Client, Cloud115Error


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.is_closed = False

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def aclose(self):
        self.is_closed = True


def response(status, payload=None, headers=None):
    return httpx.Response(
        status,
        json=payload if payload is not None else {},
        headers=headers,
        request=httpx.Request("GET", "https://aps.115.com/natsort/files.php"),
    )


class Cloud115ReadPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_client_is_reused_and_closed(self):
        client = Cloud115Client()
        first = client._shared_http()
        self.assertIs(first, client._shared_http())
        await client.close()
        self.assertTrue(first.is_closed)
        self.assertIsNot(first, client._shared_http())
        await client.close()

    async def test_transient_get_is_retried_but_state_false_fails_closed(self):
        client = Cloud115Client()
        fake = FakeHTTP([
            response(429, headers={"Retry-After": "0"}),
            response(200, {"state": True, "data": [], "count": 0}),
        ])
        client._client = fake
        with patch.object(asyncio, "sleep", new=AsyncMock()) as sleep:
            result = await client.list_dir("cid")
        self.assertEqual(result["data"], [])
        self.assertEqual(fake.calls, 2)
        sleep.assert_awaited_once()

        failed = FakeHTTP([
            response(200, {"state": False, "error": "cookie invalid", "data": []}),
        ])
        client._client = failed
        with self.assertRaises(Cloud115Error):
            await client.list_dir("cid")
        self.assertEqual(failed.calls, 1)

    async def test_transport_failure_has_finite_retry_count(self):
        client = Cloud115Client()
        request = httpx.Request("GET", "https://aps.115.com/natsort/files.php")
        fake = FakeHTTP([
            httpx.ConnectError("offline", request=request),
            httpx.ConnectError("offline", request=request),
            httpx.ConnectError("offline", request=request),
        ])
        client._client = fake
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            with self.assertRaises(Cloud115Error):
                await client.list_dir("cid")
        self.assertEqual(fake.calls, 3)


if __name__ == "__main__":
    unittest.main()
