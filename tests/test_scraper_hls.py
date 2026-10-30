import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.scraper import (
    HLSPlanError,
    HLSSourceUnavailableError,
    InvalidScrapeURL,
    VideoUnavailableError,
    _CDN_REROLL_PAGE_LOADS,
    _display_duration,
    _fetch_ok,
    _client,
    get_best_m3u8,
    _resolve_vod_plan,
    _stable_media_identity,
)


class FakeCookies:
    def __init__(self):
        self.values = []

    def set(self, *args, **kwargs):
        self.values.append((args, kwargs))


class FakePageClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cookies = FakeCookies()

    async def get(self, _url):
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class FakeMediaClient:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.requests = []

    async def get(self, url):
        self.requests.append(url)
        if url not in self.responses:
            return SimpleNamespace(status_code=404, text="", url=url)
        value = self.responses[url]
        return SimpleNamespace(status_code=200, text=value, url=url)


class HLSPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_ok_never_accepts_large_http_error_by_size(self):
        response = SimpleNamespace(
            status_code=403, text="x" * 80_000, url="https://example.test/watch")
        client = FakePageClient([response])
        with patch("backend.scraper._FETCH_MAX_TRIES", 1), \
                patch("backend.scraper.asyncio.sleep", new=AsyncMock()), \
                patch("backend.scraper._solve_challenge", return_value=None):
            self.assertIsNone(await _fetch_ok(client, response.url))

    async def test_fetch_ok_never_accepts_large_maintenance_page_by_size(self):
        response = SimpleNamespace(
            status_code=200,
            text="<title>Down for Maintenance</title>" + "x" * 80_000,
            url="https://example.test/watch",
        )
        client = FakePageClient([response])
        with patch("backend.scraper._FETCH_MAX_TRIES", 1), \
                patch("backend.scraper.asyncio.sleep", new=AsyncMock()), \
                patch("backend.scraper._solve_challenge", return_value=None):
            self.assertIsNone(await _fetch_ok(client, response.url))

    async def test_fetch_ok_solves_large_challenge_before_length_check(self):
        challenge = SimpleNamespace(
            status_code=403, text="challenge" + "x" * 80_000,
            url="https://example.test/watch",
        )
        real = SimpleNamespace(
            status_code=200, text="real" + "x" * 80_000,
            url="https://example.test/watch",
        )
        client = FakePageClient([challenge, real])
        with patch("backend.scraper._FETCH_MAX_TRIES", 2), \
                patch("backend.scraper.asyncio.sleep", new=AsyncMock()), \
                patch("backend.scraper._solve_challenge", side_effect=["cookie", None]):
            result = await _fetch_ok(client, real.url)
        self.assertIs(result, real)
        self.assertEqual(client.cookies.values[0][0][:2], ("KEY", "cookie"))

    async def test_fetch_ok_requires_page_semantics_and_final_identity(self):
        wrong = SimpleNamespace(
            status_code=200, text="wrong" + "x" * 80_000,
            url="https://example.test/watch?viewkey=wrong",
        )
        expected = SimpleNamespace(
            status_code=200, text="expected" + "x" * 80_000,
            url="https://example.test/watch?viewkey=expected",
        )
        client = FakePageClient([wrong, expected])
        with patch("backend.scraper._FETCH_MAX_TRIES", 2), \
                patch("backend.scraper.asyncio.sleep", new=AsyncMock()), \
                patch("backend.scraper._solve_challenge", return_value=None):
            result = await _fetch_ok(
                client, expected.url,
                page_validator=lambda item: "viewkey=expected" in item.url,
            )
        self.assertIs(result, expected)

    async def test_positive_page_semantics_outrank_incidental_block_words(self):
        response = SimpleNamespace(
            status_code=200,
            text=("<html><title>real video</title>" + "x" * 80_000
                  + "comment says too many requests</html>"),
            url="https://example.test/watch?viewkey=expected",
        )
        client = FakePageClient([response])
        with patch("backend.scraper._FETCH_MAX_TRIES", 1), \
                patch("backend.scraper.asyncio.sleep", new=AsyncMock()), \
                patch("backend.scraper._solve_challenge", return_value=None):
            result = await _fetch_ok(
                client, response.url, page_validator=lambda _item: True)
        self.assertIs(result, response)

    async def test_fetch_ok_returns_terminal_not_found_without_backoff(self):
        response = SimpleNamespace(
            status_code=404, text="not found", url="https://example.test/missing")
        client = FakePageClient([response])
        sleeper = AsyncMock()
        with patch("backend.scraper.asyncio.sleep", new=sleeper), \
                patch("backend.scraper._solve_challenge", return_value=None):
            self.assertIs(await _fetch_ok(client, response.url), response)
        sleeper.assert_not_awaited()

    async def test_removed_video_page_is_terminal_without_network_backoff(self):
        url = "https://cn.pornhub.com/view_video.php?viewkey=removed123"
        response = SimpleNamespace(
            status_code=200,
            text=(
                "<html><title>取消播放视频 - Pornhub.com</title>"
                "<div class='removed'>此视频已下架</div>"
                + "x" * 80_000
                + "</html>"
            ),
            url=url,
        )
        client = FakePageClient([response])
        sleeper = AsyncMock()
        with patch("backend.scraper._client", return_value=client), \
                patch("backend.scraper.asyncio.sleep", new=sleeper), \
                patch("backend.scraper._solve_challenge", return_value=None):
            with self.assertRaisesRegex(VideoUnavailableError, "此视频已下架"):
                await get_best_m3u8(url)
        sleeper.assert_not_awaited()

    async def test_geo_blocked_video_page_is_terminal_without_network_backoff(self):
        url = "https://cn.pornhub.com/view_video.php?viewkey=geoblocked1"
        response = SimpleNamespace(
            status_code=200,
            text=(
                "<html><body><div class='geoBlocked'><h1>乱码也不影响</h1></div>"
                + "x" * 80_000
                + "</body></html>"
            ),
            url=url,
        )
        client = FakePageClient([response])
        sleeper = AsyncMock()
        with patch("backend.scraper._client", return_value=client), \
                patch("backend.scraper.asyncio.sleep", new=sleeper), \
                patch("backend.scraper._solve_challenge", return_value=None):
            with self.assertRaisesRegex(VideoUnavailableError, "出口地区"):
                await get_best_m3u8(url)
        sleeper.assert_not_awaited()

    async def test_unplayable_video_title_is_terminal_without_removed_container(self):
        url = "https://cn.pornhub.com/view_video.php?viewkey=unplayable123"
        response = SimpleNamespace(
            status_code=200,
            text=(
                "<html><head><title>取消播放视频 - Pornhub.com</title>"
                "<meta property='og:title' content='取消播放视频'></head><body>"
                + "x" * 80_000
                + "</body></html>"
            ),
            url=url,
        )
        client = FakePageClient([response])
        sleeper = AsyncMock()
        with patch("backend.scraper._client", return_value=client), \
                patch("backend.scraper.asyncio.sleep", new=sleeper), \
                patch("backend.scraper._solve_challenge", return_value=None):
            with self.assertRaisesRegex(VideoUnavailableError, "取消播放视频"):
                await get_best_m3u8(url)
        sleeper.assert_not_awaited()

    async def test_page_quality_cap_filters_direct_hls_definitions(self):
        url = "https://cn.pornhub.com/view_video.php?viewkey=qualitycap123"
        html = (
            '<script>var flashvars_1 = {"video_title":"Quality cap",'
            '"video_duration":60,"mediaDefinitions":['
            '{"format":"hls","quality":"1080",'
            '"videoUrl":"https://media.example/1080.m3u8"},'
            '{"format":"hls","quality":"720",'
            '"videoUrl":"https://media.example/720.m3u8"}]};</script>'
            + "x" * 80_000
        )
        response = SimpleNamespace(status_code=200, text=html, url=url)
        client = FakePageClient([response])
        resolver = AsyncMock(return_value={
            "m3u8_url": "https://media.example/720.m3u8",
            "quality": 720,
            "manifest_text": "#EXTM3U\n#EXT-X-ENDLIST\n",
        })

        with patch("backend.scraper._client", return_value=client), patch(
                "backend.scraper.resolve_hls_plan", new=resolver):
            result = await get_best_m3u8(url, max_quality=1079)

        self.assertEqual(result["quality"], 720)
        resolver.assert_awaited_once_with(
            "https://media.example/720.m3u8",
            quality_hint=720,
            expect_audio=False,
            max_quality=1079,
        )

    @staticmethod
    def _video_page(url, definitions):
        defs = ",".join(
            f'{{"format":"hls","quality":"{quality}","videoUrl":"{media}"}}'
            for quality, media in definitions)
        html = (
            '<script>var flashvars_1 = {"video_title":"CDN reroll",'
            f'"video_duration":60,"mediaDefinitions":[{defs}]}};</script>'
            + "x" * 80_000
        )
        return SimpleNamespace(status_code=200, text=html, url=url)

    @staticmethod
    def _host_resolver(gone_host):
        async def resolve(media_url, *, quality_hint, expect_audio, max_quality):
            if gone_host in media_url:
                raise HLSSourceUnavailableError("HLS 清单请求返回 HTTP 410")
            return {"m3u8_url": media_url, "quality": quality_hint}
        return AsyncMock(side_effect=resolve)

    async def test_ph_cdn_410_reloads_page_instead_of_degrading_quality(self):
        url = "https://cn.pornhub.com/view_video.php?viewkey=reroll123"
        hv_1080 = "https://hv-h.phncdn.com/hls/v/1080.mp4/master.m3u8?h=a&e=1&f=1"
        ev_720 = "https://ev-h.phncdn.com/hls/v/720.mp4/master.m3u8?hash=b"
        ev_1080 = "https://ev-h.phncdn.com/hls/v/1080.mp4/master.m3u8?hash=c"
        client = FakePageClient([
            self._video_page(url, [("1080", hv_1080), ("720", ev_720)]),
            self._video_page(url, [("1080", hv_1080)]),
            self._video_page(url, [("1080", ev_1080), ("720", ev_720)]),
        ])
        resolver = self._host_resolver("hv-h.phncdn.com")
        sleeper = AsyncMock()

        with patch("backend.scraper._client", return_value=client), \
                patch("backend.scraper.resolve_hls_plan", new=resolver), \
                patch("backend.scraper.asyncio.sleep", new=sleeper):
            result = await get_best_m3u8(url)

        self.assertEqual((result["m3u8_url"], result["quality"]), (ev_1080, 1080))
        # 第二次页面仍是已确认 410 的主机，不再重复请求清单；同页 720 也不被使用。
        self.assertEqual(
            [call.args[0] for call in resolver.await_args_list], [hv_1080, ev_1080])
        self.assertEqual(sleeper.await_count, 2)
        self.assertEqual(client.responses, [])

    async def test_ph_cdn_410_on_every_reload_keeps_source_unavailable(self):
        url = "https://cn.pornhub.com/view_video.php?viewkey=allgone123"
        hv_1080 = "https://hv-h.phncdn.com/hls/v/1080.mp4/master.m3u8?h=a&e=1&f=1"
        client = FakePageClient([
            self._video_page(url, [("1080", hv_1080)])
            for _ in range(_CDN_REROLL_PAGE_LOADS + 1)])
        resolver = self._host_resolver("hv-h.phncdn.com")

        with patch("backend.scraper._client", return_value=client), \
                patch("backend.scraper.resolve_hls_plan", new=resolver), \
                patch("backend.scraper.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(HLSSourceUnavailableError):
                await get_best_m3u8(url)

        resolver.assert_awaited_once()
        self.assertEqual(len(client.responses), 1)   # 恰好用完重抽次数

    async def test_410_from_two_ph_cdn_hosts_stops_rerolling(self):
        url = "https://cn.pornhub.com/view_video.php?viewkey=gone123"
        hv_1080 = "https://hv-h.phncdn.com/hls/v/1080.mp4/master.m3u8?h=a&e=1&f=1"
        ev_1080 = "https://ev-h.phncdn.com/hls/v/1080.mp4/master.m3u8?hash=c"
        client = FakePageClient([
            self._video_page(url, [("1080", hv_1080)]),
            self._video_page(url, [("1080", ev_1080)]),
            self._video_page(url, [("1080", ev_1080)]),
        ])
        resolver = self._host_resolver(".phncdn.com")

        with patch("backend.scraper._client", return_value=client), \
                patch("backend.scraper.resolve_hls_plan", new=resolver), \
                patch("backend.scraper.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(HLSSourceUnavailableError):
                await get_best_m3u8(url)

        self.assertEqual(resolver.await_count, 2)
        self.assertEqual(len(client.responses), 1)

    async def test_non_ph_cdn_410_is_not_rerolled(self):
        url = "https://cn.pornhub.com/view_video.php?viewkey=foreign123"
        media = "https://media.example/1080.m3u8"
        client = FakePageClient([
            self._video_page(url, [("1080", media)]),
            self._video_page(url, [("1080", media)]),
        ])
        resolver = self._host_resolver("media.example")

        with patch("backend.scraper._client", return_value=client), \
                patch("backend.scraper.resolve_hls_plan", new=resolver):
            with self.assertRaises(HLSSourceUnavailableError):
                await get_best_m3u8(url)

        resolver.assert_awaited_once()
        self.assertEqual(len(client.responses), 1)

    def test_dirty_page_duration_is_display_only(self):
        self.assertEqual(_display_duration("10.9"), 10)
        self.assertEqual(_display_duration("not-a-number"), 0)
        self.assertEqual(_display_duration("nan"), 0)

    def test_media_manifest_uses_download_proxy_not_scraper_proxy(self):
        from backend.config import settings

        old_scraper_proxy = settings.proxy
        old_download_proxy = settings.download_proxy
        try:
            settings.proxy = "http://127.0.0.1:7890"
            settings.download_proxy = "http://127.0.0.1:7891"
            with patch("backend.scraper.httpx.AsyncClient") as constructor:
                _client(ph_only=False, media_path=True)
            self.assertEqual(
                constructor.call_args.kwargs["proxy"],
                "http://127.0.0.1:7891",
            )
        finally:
            settings.proxy = old_scraper_proxy
            settings.download_proxy = old_download_proxy

    async def test_nested_master_resolves_highest_variant_to_leaf_vod(self):
        root = "https://media.example/master.m3u8"
        high = "https://media.example/1080/master.m3u8"
        leaf = "https://media.example/1080/leaf.m3u8"
        client = FakeMediaClient({
            root: (
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720\n"
                "720/leaf.m3u8\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1920x1080,"
                "CODECS=\"avc1.640028,mp4a.40.2\"\n"
                "1080/master.m3u8\n"
            ),
            high: (
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=2300000,RESOLUTION=1920x1080,"
                "CODECS=\"avc1.640028,mp4a.40.2\"\n"
                "leaf.m3u8\n"
            ),
            leaf: (
                "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
                "#EXT-X-MEDIA-SEQUENCE:7\n"
                "#EXTINF:9.5,\nseg-001.ts?hash=one&track=main\n"
                "#EXTINF:9.25,\nseg-002.ts?hash=two&track=main\n"
                "#EXT-X-ENDLIST\n"
            ),
        })

        plan = await _resolve_vod_plan(client, root)

        self.assertEqual(plan["m3u8_url"], leaf)
        self.assertEqual(plan["quality"], 1080)
        self.assertEqual(plan["segment_count"], 2)
        self.assertEqual(plan["unit_count"], 2)
        self.assertAlmostEqual(plan["media_duration"], 18.75)
        self.assertEqual(plan["target_duration"], 10)
        self.assertEqual(plan["media_sequence"], 7)
        self.assertTrue(plan["expect_audio"])
        self.assertRegex(plan["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(client.requests, [root, high, leaf])

    async def test_master_quality_cap_skips_known_bad_higher_variant(self):
        root = "https://media.example/master.m3u8"
        lower = "https://media.example/720/leaf.m3u8"
        client = FakeMediaClient({
            root: (
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1920x1080\n"
                "1080/leaf.m3u8\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720\n"
                "720/leaf.m3u8\n"
            ),
            lower: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                "#EXTINF:6,\nsegment.ts\n#EXT-X-ENDLIST\n"
            ),
        })

        plan = await _resolve_vod_plan(client, root, max_quality=720)

        self.assertEqual(plan["m3u8_url"], lower)
        self.assertEqual(plan["quality"], 720)
        self.assertEqual(client.requests, [root, lower])

    async def test_leaf_without_endlist_is_deterministic_error(self):
        url = "https://media.example/live.m3u8"
        client = FakeMediaClient({
            url: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                "#EXTINF:6,\nsegment.ts\n"
            ),
        })
        with self.assertRaisesRegex(HLSPlanError, "ENDLIST"):
            await _resolve_vod_plan(client, url)

    async def test_empty_leaf_is_rejected(self):
        url = "https://media.example/empty.m3u8"
        client = FakeMediaClient({
            url: "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-ENDLIST\n",
        })
        with self.assertRaisesRegex(HLSPlanError, "没有可下载分片"):
            await _resolve_vod_plan(client, url)

    async def test_master_cycle_is_rejected(self):
        first = "https://media.example/first.m3u8"
        second = "https://media.example/second.m3u8"
        client = FakeMediaClient({
            first: "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nsecond.m3u8\n",
            second: "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nfirst.m3u8\n",
        })
        with self.assertRaisesRegex(HLSPlanError, "循环"):
            await _resolve_vod_plan(client, first)

    async def test_ambiguous_external_audio_is_rejected_instead_of_silently_lost(self):
        url = "https://media.example/master.m3u8"
        client = FakeMediaClient({
            url: (
                "#EXTM3U\n"
                "#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"aud\",NAME=\"main\","
                "DEFAULT=YES,URI=\"audio.m3u8\"\n"
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="alt",DEFAULT=YES,URI="alt.m3u8"\n'
                "#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=1920x1080,"
                "AUDIO=\"aud\",CODECS=\"avc1.640028,mp4a.40.2\"\n"
                "video.m3u8\n"
            ),
        })
        with self.assertRaisesRegex(HLSPlanError, "外置音轨"):
            await _resolve_vod_plan(client, url)

    async def test_master_falls_back_from_ambiguous_audio_to_muxed_variant(self):
        root = "https://media.example/master.m3u8"
        lower = "https://media.example/720.m3u8"
        client = FakeMediaClient({
            root: (
                "#EXTM3U\n"
                "#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"aud\",NAME=\"main\","
                "DEFAULT=YES,URI=\"audio.m3u8\"\n"
                '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="alt",DEFAULT=YES,URI="alt.m3u8"\n'
                "#EXT-X-STREAM-INF:BANDWIDTH=3000,RESOLUTION=1920x1080,"
                "AUDIO=\"aud\",CODECS=\"avc1.640028,mp4a.40.2\"\n"
                "1080.m3u8\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=1500,RESOLUTION=1280x720,"
                "CODECS=\"avc1.4d401f,mp4a.40.2\"\n"
                "720.m3u8\n"
            ),
            lower: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                "#EXTINF:6,\nsegment.ts\n#EXT-X-ENDLIST\n"
            ),
        })

        plan = await _resolve_vod_plan(client, root)

        self.assertEqual(plan["m3u8_url"], lower)
        self.assertEqual(plan["quality"], 720)
        self.assertTrue(plan["expect_audio"])
        self.assertEqual(client.requests, [root, lower])

    async def test_direct_leaf_audio_requirement_is_unknown_not_forced(self):
        url = "https://media.example/leaf.m3u8"
        client = FakeMediaClient({
            url: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                "#EXTINF:6,\nsegment.ts\n#EXT-X-ENDLIST\n"
            ),
        })

        plan = await _resolve_vod_plan(client, url)

        self.assertFalse(plan["expect_audio"])

    def test_stable_media_identity_ignores_only_rotating_signature(self):
        first = _stable_media_identity(
            "https://a-h.phncdn.com/signed-one,1/hls/video/seg.ts"
            "?validfrom=1&validto=2&hash=aaa&track=main")
        second = _stable_media_identity(
            "https://b-h.phncdn.com/signed-two,2/hls/video/seg.ts"
            "?validfrom=3&validto=4&hash=bbb&track=main")
        changed = _stable_media_identity(
            "https://b-h.phncdn.com/signed-two,2/hls/video/seg.ts"
            "?validfrom=3&validto=4&hash=bbb&track=alternate")

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_non_ph_query_token_remains_part_of_identity(self):
        first = _stable_media_identity(
            "https://media.example/hls/seg.ts?token=content-a&track=main")
        second = _stable_media_identity(
            "https://media.example/hls/seg.ts?token=content-b&track=main")
        self.assertNotEqual(first, second)

    def test_ph_signature_families_share_one_media_identity(self):
        ev = _stable_media_identity(
            "https://ev-h.phncdn.com/hls/c6251/videos/2025/1/video.mp4/seg.ts"
            "?validfrom=1&validto=2&ipa=1&hdl=-1&hash=aaa")
        iv = _stable_media_identity(
            "https://iv-h.phncdn.com/signed,2/hls/c6251/videos/2025/1/"
            "video.mp4/seg.ts")
        hv = _stable_media_identity(
            "https://hv-h.phncdn.com/hls/c6251/videos/2025/1/video.mp4/seg.ts"
            "?h=bbb&e=2&f=1")
        self.assertEqual(ev, iv)
        self.assertEqual(iv, hv)

    async def test_target_duration_is_required_without_fallback(self):
        url = "https://media.example/no-target.m3u8"
        client = FakeMediaClient({
            url: "#EXTM3U\n#EXTINF:6,\nsegment.ts\n#EXT-X-ENDLIST\n",
        })
        with self.assertRaisesRegex(HLSPlanError, "TARGETDURATION"):
            await _resolve_vod_plan(client, url)

    async def test_non_finite_segment_duration_is_rejected(self):
        url = "https://media.example/nan.m3u8"
        client = FakeMediaClient({
            url: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                "#EXTINF:nan,\nsegment.ts\n#EXT-X-ENDLIST\n"
            ),
        })
        with self.assertRaisesRegex(HLSPlanError, "无效分片时长"):
            await _resolve_vod_plan(client, url)

    async def test_private_segment_uri_is_rejected(self):
        url = "https://media.example/private-segment.m3u8"
        client = FakeMediaClient({
            url: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                "#EXTINF:6,\nhttp://127.0.0.1/private.ts\n#EXT-X-ENDLIST\n"
            ),
        })
        with self.assertRaises(InvalidScrapeURL):
            await _resolve_vod_plan(client, url)

    async def test_private_key_and_map_uris_are_rejected(self):
        key_url = "https://media.example/private-key.m3u8"
        map_url = "https://media.example/private-map.m3u8"
        client = FakeMediaClient({
            key_url: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                '#EXT-X-KEY:METHOD=AES-128,URI="http://127.0.0.1/key"\n'
                "#EXTINF:6,\nsegment.ts\n#EXT-X-ENDLIST\n"
            ),
            map_url: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                '#EXT-X-MAP:URI="http://127.0.0.1/init.mp4"\n'
                "#EXTINF:6,\nsegment.m4s\n#EXT-X-ENDLIST\n"
            ),
        })
        with self.assertRaises(InvalidScrapeURL):
            await _resolve_vod_plan(client, key_url)
        with self.assertRaises(InvalidScrapeURL):
            await _resolve_vod_plan(client, map_url)

    async def test_unsupported_encryption_is_rejected_before_worker(self):
        url = "https://media.example/sample-aes.m3u8"
        client = FakeMediaClient({
            url: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n'
                "#EXTINF:6,\nsegment.ts\n#EXT-X-ENDLIST\n"
            ),
        })
        with self.assertRaisesRegex(HLSPlanError, "不支持加密方法"):
            await _resolve_vod_plan(client, url)

    async def test_declared_discontinuity_is_split_into_complete_periods(self):
        url = "https://media.example/discontinuous.m3u8"
        client = FakeMediaClient({
            url: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                "#EXTINF:6,\nsegment-1.ts\n"
                "#EXT-X-DISCONTINUITY\n"
                "#EXTINF:6,\nsegment-2.ts\n#EXT-X-ENDLIST\n"
            ),
        })
        plan = await _resolve_vod_plan(client, url)
        self.assertEqual(plan["schema_version"], 3)
        self.assertEqual(plan["media_duration"], 12)
        self.assertEqual(len(plan["tracks"][0]["parts"]), 2)

    async def test_repeated_map_is_rejected_before_native_download(self):
        url = "https://media.example/repeated-map.m3u8"
        client = FakeMediaClient({
            url: (
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
                '#EXT-X-MAP:URI="init.mp4"\n'
                "#EXTINF:6,\nsegment-1.m4s\n"
                '#EXT-X-MAP:URI="init.mp4"\n'
                "#EXTINF:6,\nsegment-2.m4s\n#EXT-X-ENDLIST\n"
            ),
        })
        with self.assertRaisesRegex(HLSPlanError, "EXT-X-MAP"):
            await _resolve_vod_plan(client, url)


if __name__ == "__main__":
    unittest.main()
