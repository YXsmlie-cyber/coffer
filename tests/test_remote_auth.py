from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
import unittest

import httpx

from backend import remote_auth
from backend.main import app


class RemoteSessionTokenTests(unittest.TestCase):
    def test_token_round_trip_tamper_and_expiry(self):
        secret = b"test-secret" * 4
        token, expires_at, max_age = remote_auth.issue_session(
            True, now=1_000, secret=secret)

        self.assertEqual(expires_at, 1_000 + max_age)
        self.assertEqual(
            remote_auth.session_expiry(token, now=1_001, secret=secret),
            expires_at,
        )
        self.assertIsNone(remote_auth.session_expiry(
            token + "x", now=1_001, secret=secret))
        self.assertIsNone(remote_auth.session_expiry(
            token, now=expires_at, secret=secret))

    def test_cookie_path_only_accepts_safe_proxy_prefix(self):
        self.assertEqual(remote_auth.cookie_path(
            {"x-forwarded-prefix": "/ph115/"}), "/ph115")
        self.assertEqual(remote_auth.cookie_path(
            {"x-forwarded-prefix": "https://attacker.invalid"}), "/")
        self.assertEqual(remote_auth.cookie_path({}), "/")

    def test_proxy_verification_requires_loopback_source(self):
        headers = {remote_auth.VERIFIED_HEADER: "1"}
        self.assertTrue(remote_auth.proxy_verified(headers, "127.0.0.1"))
        self.assertTrue(remote_auth.proxy_verified(headers, "::1"))
        self.assertFalse(remote_auth.proxy_verified(headers, "203.0.113.10"))
        self.assertFalse(remote_auth.proxy_verified({}, "127.0.0.1"))
        self.assertTrue(remote_auth.proxy_header_verified(headers))
        self.assertFalse(remote_auth.proxy_source_verified("203.0.113.10"))


class RemoteSessionHttpTests(unittest.IsolatedAsyncioTestCase):
    secret = b"http-test-secret" * 4
    remote_headers = {
        "host": "remote.example.test",
        "x-ph115-remote": "1",
        "x-forwarded-prefix": "/ph115",
        "x-forwarded-proto": "https",
    }

    async def asyncSetUp(self):
        self.transport = httpx.ASGITransport(
            app=app, client=("127.0.0.1", 54321))
        self.client = httpx.AsyncClient(
            transport=self.transport,
            base_url="https://remote.example.test",
            headers=self.remote_headers,
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_remote_api_requires_session_and_login_sets_secure_cookie(self):
        status = await self.client.get("/api/remote-auth/status")
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["required"])
        self.assertFalse(status.json()["authenticated"])
        self.assertEqual(status.headers["cache-control"], "no-store")

        protected = await self.client.get("/api/patrol/status")
        self.assertEqual(protected.status_code, 401)
        self.assertEqual(
            protected.json()["code"], "REMOTE_AUTH_REQUIRED")

        denied = await self.client.post(
            "/api/remote-auth/login", json={"remember": True})
        self.assertEqual(denied.status_code, 403)

        with patch("backend.remote_auth.session_secret", return_value=self.secret):
            login = await self.client.post(
                "/api/remote-auth/login",
                json={"remember": True},
                headers={remote_auth.VERIFIED_HEADER: "1"},
            )
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()["authenticated"])

        set_cookie = login.headers["set-cookie"]
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("Secure", set_cookie)
        self.assertIn("SameSite=lax", set_cookie)
        self.assertIn("Path=/ph115", set_cookie)
        self.assertIn("Max-Age=", set_cookie)
        parsed = SimpleCookie()
        parsed.load(set_cookie)
        token = parsed[remote_auth.COOKIE_NAME].value

        with patch("backend.remote_auth.session_secret", return_value=self.secret):
            allowed = await self.client.get(
                "/api/patrol/status",
                headers={"cookie": f"{remote_auth.COOKIE_NAME}={token}"},
            )
        self.assertEqual(allowed.status_code, 200)

        logout = await self.client.post("/api/remote-auth/logout")
        self.assertEqual(logout.status_code, 200)
        self.assertIn("Path=/ph115", logout.headers["set-cookie"])
        self.assertIn("Max-Age=0", logout.headers["set-cookie"])

    async def test_spoofed_verified_header_from_public_client_is_rejected(self):
        transport = httpx.ASGITransport(
            app=app, client=("203.0.113.10", 54321))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://remote.example.test",
            headers=self.remote_headers,
        ) as client:
            response = await client.post(
                "/api/remote-auth/login",
                json={"remember": True},
                headers={remote_auth.VERIFIED_HEADER: "1"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("来源不受信任", response.json()["detail"])

    async def test_loopback_access_does_not_require_remote_login(self):
        transport = httpx.ASGITransport(
            app=app, client=("127.0.0.1", 54321))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as client:
            status = await client.get("/api/remote-auth/status")
            protected = await client.get("/api/patrol/status")

        self.assertFalse(status.json()["required"])
        self.assertTrue(status.json()["authenticated"])
        self.assertEqual(protected.status_code, 200)


class RemoteNginxConfigTests(unittest.TestCase):
    def test_only_exact_login_location_uses_basic_auth(self):
        config = (Path(__file__).parents[1]
                  / "deploy" / "remote-access"
                  / "nginx-ph115-location.conf").read_text(encoding="utf-8")

        exact, broad = config.split("location ^~ /ph115/ {", 1)
        self.assertIn("location = /ph115/api/remote-auth/login", exact)
        self.assertIn('auth_basic "PH115 Remote Access";', exact)
        self.assertNotIn("auth_basic", broad)
        self.assertIn("proxy_set_header X-PH115-Auth-Verified 1;", exact)
        self.assertIn('proxy_set_header X-PH115-Auth-Verified "";', broad)
        self.assertGreaterEqual(
            config.count('proxy_set_header Authorization "";'), 2)
        self.assertIn('code\":\"REMOTE_LOGIN_FAILED', config)
        self.assertIn("return 403", config)


if __name__ == "__main__":
    unittest.main()
