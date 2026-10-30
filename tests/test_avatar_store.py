import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from backend import avatar_store


def _jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (320, 200), (80, 120, 180)).save(output, format="JPEG")
    return output.getvalue()


class AvatarStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_avatar_transcodes_and_uses_content_hash(self):
        source = _jpeg_bytes()

        def handler(request: httpx.Request):
            return httpx.Response(
                200, headers={"content-type": "image/jpeg"},
                content=source, request=request)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(avatar_store.settings, "avatar_dir", directory), \
                patch.object(avatar_store.settings, "avatar_max_bytes", 5 * 1024 * 1024):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                asset = await avatar_store.cache_avatar(
                    "https://ei.phncdn.com/avatar.jpg", client=client)
            path = Path(directory) / f"{asset.sha256}.webp"
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), asset.sha256)
            with Image.open(path) as image:
                self.assertEqual(image.size, (256, 256))
                self.assertEqual(image.format, "WEBP")

    async def test_invalid_image_is_rejected_without_creating_asset(self):
        calls = 0

        def handler(request: httpx.Request):
            nonlocal calls
            calls += 1
            return httpx.Response(
                200, headers={"content-type": "image/jpeg"},
                content=b"not-an-image", request=request)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(avatar_store.settings, "avatar_dir", directory):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(avatar_store.AvatarStoreError):
                    await avatar_store.cache_avatar(
                        "https://ei.phncdn.com/avatar.jpg", client=client)
            self.assertEqual(list(Path(directory).glob("*.webp")), [])
            self.assertEqual(calls, 1)

    async def test_transient_download_error_is_retried(self):
        source = _jpeg_bytes()
        calls = 0

        def handler(request: httpx.Request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ReadError("temporary disconnect", request=request)
            return httpx.Response(
                200, headers={"content-type": "image/jpeg"},
                content=source, request=request)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(avatar_store.settings, "avatar_dir", directory):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                asset = await avatar_store.cache_avatar(
                    "https://ei.phncdn.com/avatar.jpg", client=client)
            self.assertEqual(calls, 2)
            self.assertTrue((Path(directory) / f"{asset.sha256}.webp").is_file())


if __name__ == "__main__":
    unittest.main()
