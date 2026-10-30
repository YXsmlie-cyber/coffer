import unittest
from unittest.mock import AsyncMock, patch

from backend import cloud115
from backend.cloud115 import Cloud115Error


class Cloud115RuntimeTests(unittest.TestCase):
    def test_upload_error_chain_preserves_cause_but_redacts_credentials(self):
        class MultipartUploadAbort(Exception):
            pass

        try:
            try:
                raise RuntimeError("init HTTP 503 token=abc123")
            except RuntimeError as cause:
                raise MultipartUploadAbort({
                    "user_key": "super-secret-user-key",
                    "callback": "https://callback.invalid/?token=secret",
                }) from cause
        except MultipartUploadAbort as error:
            detail = cloud115._safe_exception_chain(error)

        self.assertIn("MultipartUploadAbort: 上传初始化或分片流程中止", detail)
        self.assertIn("RuntimeError: init HTTP 503", detail)
        self.assertNotIn("super-secret-user-key", detail)
        self.assertNotIn("abc123", detail)

    def test_upload_error_chain_names_oss_host_and_error_code(self):
        """403 要带出 OSS 错误码，才能区分签名过期、时间偏差和权限问题。"""
        import io
        from email.message import Message
        from urllib.error import HTTPError

        class MultipartUploadAbort(Exception):
            pass

        class _Body(io.BytesIO):
            data = (
                b"<?xml version='1.0'?><Error><Code>RequestTimeTooSkewed</Code>"
                b"<Message>The difference between the request time and the current time "
                b"is too large.</Message><RequestId>abc</RequestId></Error>"
            )

        headers = Message()
        headers["x-oss-ec"] = "0002-00000504"
        cause = HTTPError(
            "http://fhnfile.oss-cn-shenzhen.aliyuncs.com/6ae0object"
            "?partNumber=3&uploadId=secret-upload-id",
            403, "Forbidden", headers, _Body(),
        )
        try:
            try:
                raise cause
            except HTTPError as error:
                raise MultipartUploadAbort({"upload_id": "secret-upload-id"}) from error
        except MultipartUploadAbort as error:
            detail = cloud115._safe_exception_chain(error)

        self.assertIn("HTTP Error 403: Forbidden", detail)
        self.assertIn("fhnfile.oss-cn-shenzhen.aliyuncs.com", detail)
        self.assertIn("OSS RequestTimeTooSkewed", detail)
        self.assertIn("EC 0002-00000504", detail)
        self.assertNotIn("secret-upload-id", detail)
        self.assertNotIn("partNumber", detail)

    def test_historical_upload_error_text_is_redacted(self):
        detail = cloud115.redact_sensitive_error_text(
            "MultipartUploadAbort: {'user_key': 'secret-key', "
            "'callback': None, 'token': abc123}"
        )
        self.assertNotIn("secret-key", detail)
        self.assertNotIn("abc123", detail)
        self.assertGreaterEqual(detail.count("<redacted>"), 3)

    def test_truncated_upload_init_response_is_retryable_only_before_transfer(self):
        class MultipartUploadAbort(Exception):
            pass

        try:
            try:
                raise IndexError("index out of bounds on dimension 1")
            except IndexError as cause:
                raise MultipartUploadAbort({
                    "upload_id": "",
                    "callback": None,
                    "user_key": "secret",
                }) from cause
        except MultipartUploadAbort as error:
            self.assertTrue(cloud115._is_truncated_upload_init_error(error))
            detail = cloud115._safe_exception_chain(error)
        self.assertIn("初始化响应为空或截断", detail)
        self.assertNotIn("secret", detail)

        try:
            try:
                raise IndexError("index out of bounds on dimension 1")
            except IndexError as cause:
                raise MultipartUploadAbort({
                    "upload_id": "already-started",
                    "callback": {"bucket": "value"},
                }) from cause
        except MultipartUploadAbort as error:
            self.assertFalse(cloud115._is_truncated_upload_init_error(error))

    def test_pinned_upload_runtime_can_import(self):
        """上传硬依赖必须真正可导入，不能只满足 pip 元数据。"""
        client_class = cloud115._p115_client_class()

        self.assertEqual(client_class.__name__, "P115Client")

    def test_transitive_import_error_is_not_misreported_as_uninstalled(self):
        with patch.object(
            cloud115,
            "import_module",
            side_effect=ImportError("cannot import name 'to_list'"),
        ):
            with self.assertRaisesRegex(
                Cloud115Error, "依赖版本不兼容.*to_list"
            ):
                cloud115._p115_client_class()

    def test_missing_transitive_dependency_is_named(self):
        error = ModuleNotFoundError("No module named 'example_dep'")
        error.name = "example_dep"
        with patch.object(cloud115, "import_module", side_effect=error):
            with self.assertRaisesRegex(Cloud115Error, "依赖缺失: example_dep"):
                cloud115._p115_client_class()


class Plugin115BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_plugin_directory_check_returns_only_ready_status(self):
        client = cloud115.Cloud115Client()
        client._cookie = "COOKIE_SENTINEL_DO_NOT_ECHO"
        with patch.object(client, "list_dir", new=AsyncMock(return_value={"state": True, "data": []})) as listed:
            result = await client.plugin_directory_ready("12345")

        self.assertEqual(result, {"status": "READY"})
        listed.assert_awaited_once_with("12345", limit=1)
        self.assertNotIn(client._cookie, repr(result))

    async def test_plugin_submit_uses_minimal_payload_and_hides_magnet(self):
        client = cloud115.Cloud115Client()
        magnet = "magnet:?xt=urn:btih:" + "a" * 40
        payloads: list[dict] = []

        class _P115:
            def clouddownload_task_add_url(self, payload):
                payloads.append(payload)
                return {"state": True}

        with patch.object(client, "plugin_directory_ready", new=AsyncMock(return_value={"status": "READY"})), \
                patch.object(cloud115, "_get_p115", return_value=_P115()):
            result = await client.plugin_submit_magnet(magnet, "12345")

        self.assertEqual(result, {"status": "CONFIRMED_SUCCESS"})
        self.assertEqual(payloads, [{"url": magnet, "wp_path_id": "12345"}])
        self.assertNotIn(magnet, repr(result))


if __name__ == "__main__":
    unittest.main()
