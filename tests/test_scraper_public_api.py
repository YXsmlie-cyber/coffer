"""封面调用方不需要知道 DOM 与 flashvars 的实现细节。"""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend import scraper


class PublicPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_poster_returns_validated_image_and_exact_video_referer(self):
        url = "https://cn.pornhub.com/view_video.php?viewkey=abc123def4567"
        response = SimpleNamespace(status_code=200, url=url,
            text='var flashvars_123 = {"image_url":"https://cdn.example/poster.jpg"};')
        fetch = AsyncMock(return_value=response)
        with patch.object(scraper, "_fetch_ok", fetch):
            result = await scraper.resolve_video_poster(object(), "abc123def4567")
        self.assertEqual(result, ("https://cdn.example/poster.jpg", url))
        self.assertTrue(fetch.call_args.kwargs["page_validator"](response))

    async def test_wrong_video_or_private_media_never_escapes_boundary(self):
        response = SimpleNamespace(status_code=200,
            url="https://cn.pornhub.com/view_video.php?viewkey=other1234567",
            text='var flashvars_123 = {"image_url":"https://cdn.example/poster.jpg"};')
        with patch.object(scraper, "_fetch_ok", AsyncMock(return_value=response)):
            self.assertIsNone(await scraper.resolve_video_poster(object(), "abc123def4567"))
            response.url = "https://cn.pornhub.com/view_video.php?viewkey=abc123def4567"
            response.text = 'var flashvars_123 = {"image_url":"http://127.0.0.1/private"};'
            with self.assertRaises(scraper.InvalidScrapeURL):
                await scraper.resolve_video_poster(object(), "abc123def4567")

    async def test_missing_poster_and_unavailable_page_keep_distinct_results(self):
        response = SimpleNamespace(status_code=200,
            url="https://cn.pornhub.com/view_video.php?viewkey=abc123def4567",
            text='var flashvars_123 = {"image_url":""};')
        with patch.object(scraper, "_fetch_ok", AsyncMock(return_value=response)):
            self.assertEqual(await scraper.resolve_video_poster(object(), "abc123def4567"), ("", ""))
            response.status_code = 404
            self.assertIsNone(await scraper.resolve_video_poster(object(), "abc123def4567"))
