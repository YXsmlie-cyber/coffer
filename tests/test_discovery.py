from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import discovery, model_sync, scraper
from backend.database import Base, DiscoveryItem, Model, Video
import backend.main as main_module
from api_patch import patch_api


class DiscoveryParserTests(unittest.TestCase):
    def test_catalog_contains_home_and_all_video_menu_sources(self):
        keys = [item["key"] for item in scraper.discovery_source_catalog()]
        self.assertEqual(keys, [
            "home", "recommended", "hot", "most_viewed", "top_rated",
            "homemade", "shorties", "playlists", "channels", "random",
            "latest", "viewers_choice",
        ])

    def test_video_card_keeps_cover_and_namespaced_channel_identity(self):
        soup = BeautifulSoup("""
          <li class="pcVideoListItem">
            <a href="/view_video.php?viewkey=abcdef1234567" title="Sample">
              <img data-src="https://cdn.example/cover.jpg">
            </a>
            <var class="duration">12:34</var>
            <div class="videoUploaderBlock"><span class="usernameWrap">
              <a href="/channels/sample-channel">Sample Channel</a>
            </span></div>
          </li>
        """, "lxml")
        parsed = scraper._parse_discovery_video_card(
            soup.select_one("li"), "https://cn.pornhub.com/video")
        self.assertEqual(parsed["viewkey"], "abcdef1234567")
        self.assertEqual(parsed["duration"], 754)
        self.assertEqual(parsed["thumbnail"], "https://cdn.example/cover.jpg")
        self.assertEqual(parsed["creator"]["profile_type"], "channel")
        self.assertEqual(parsed["creator"]["ph_id"], "channel:sample-channel")

    def test_shorties_embedded_json_is_parsed_without_dom_cards(self):
        html = """
          <div id="shorties"></div>
          <script>
            JSON_SHORTIES = insertAfterNthPosition(\n              [{
              "vkey":"abcdef1234567", "videoTitle":"Sample Short",
              "profileUrl":"/model/sample-model", "name":"Sample Model",
              "largePreviewUrl":"https://cdn.example/short.jpg"
            }], 8);
          </script>
        """
        items = scraper._parse_shorties(html, "https://cn.pornhub.com/shorties")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["viewkey"], "abcdef1234567")
        self.assertEqual(items[0]["creator"]["ph_id"], "sample-model")
        self.assertEqual(items[0]["thumbnail"], "https://cdn.example/short.jpg")

    def test_trusted_source_url_only_accepts_ph_publisher_profiles(self):
        self.assertEqual(
            scraper.validate_trusted_source_url(
                "https://cn.pornhub.com/channels/sample-studio/videos"),
            "https://cn.pornhub.com/channels/sample-studio/videos",
        )
        with self.assertRaises(scraper.InvalidScrapeURL):
            scraper.validate_trusted_source_url(
                "https://cn.pornhub.com/view_video.php?viewkey=abcdef1234567")
        with self.assertRaises(scraper.InvalidScrapeURL):
            scraper.validate_trusted_source_url("https://example.com/channels/sample")


class _DiscoveryClientContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _DiscoveryResponse:
    def __init__(self, url: str, html: str):
        self.url = url
        self.text = html
        self.status_code = 200


class DiscoveryPaginationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def card(viewkey: str, slug: str, name: str) -> str:
        return f"""
          <li class="pcVideoListItem">
            <a href="/view_video.php?viewkey={viewkey}" title="{name}">
              <img data-src="https://cdn.example/{viewkey}.jpg">
            </a>
            <var class="duration">1:00</var>
            <div class="videoUploaderBlock"><span class="usernameWrap">
              <a href="/model/{slug}">{name}</a>
            </span></div>
          </li>
        """

    async def test_home_follows_authoritative_next_link_until_source_is_exhausted(self):
        first = _DiscoveryResponse(
            "https://cn.pornhub.com/",
            '<link rel="next" href="/video?page=2"><ul>'
            + self.card("aaa1111111111", "first", "First") + "</ul>",
        )
        second = _DiscoveryResponse(
            "https://cn.pornhub.com/video?page=2",
            "<ul>" + self.card("bbb2222222222", "second", "Second") + "</ul>",
        )
        fetch = AsyncMock(side_effect=[first, second])
        with patch.object(scraper, "_client", return_value=_DiscoveryClientContext()), \
                patch.object(scraper, "_fetch_ok", new=fetch), \
                patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            result = await scraper.scrape_discovery("home", 10)

        self.assertEqual(fetch.await_args_list[1].args[1],
                         "https://cn.pornhub.com/video?page=2")
        self.assertEqual(result["creator_count"], 2)
        self.assertEqual(result["page_count"], 2)
        self.assertFalse(result["reached_limit"])

    async def test_recommended_source_enables_personalization_cookie_only_for_its_client(self):
        response = _DiscoveryResponse(
            "https://cn.pornhub.com/recommended",
            "<ul id='videoCategory'>"
            + self.card("aaa1111111111", "first", "First") + "</ul>",
        )
        fetch = AsyncMock(return_value=response)
        with patch.object(
                scraper, "_client", return_value=_DiscoveryClientContext()) as client_factory, \
                patch.object(scraper, "_fetch_ok", new=fetch):
            result = await scraper.scrape_discovery("recommended", 1)

        client_factory.assert_called_once_with(personalized_recommendations=True)
        self.assertEqual(result["creator_count"], 1)

    async def test_long_recommended_pagination_renews_budget_after_verified_page(self):
        first = _DiscoveryResponse(
            "https://cn.pornhub.com/recommended",
            "<ul id='videoCategory'>"
            + self.card("aaa1111111111", "first", "First") + "</ul>",
        )
        second = _DiscoveryResponse(
            "https://cn.pornhub.com/recommended?page=2",
            "<ul id='videoCategory'>"
            + self.card("bbb2222222222", "second", "Second") + "</ul>",
        )
        calls = []

        async def fetch(_client, url, *, budget=None):
            calls.append(url)
            if len(calls) == 1:
                # 模拟前一页网络和解析刚好用尽墙钟窗口；可信进展应只续期
                # deadline，不恢复共享重试次数。
                budget.retries_remaining -= 1
                budget.deadline = 0.0
                return first
            self.assertGreater(budget.remaining_seconds(), 0)
            self.assertEqual(budget.retries_remaining, scraper._FETCH_MAX_TRIES - 2)
            return second

        with patch.object(scraper, "_client", return_value=_DiscoveryClientContext()), \
                patch.object(scraper, "_fetch_ok", new=fetch), \
                patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            result = await scraper.scrape_discovery("recommended", 2)

        self.assertEqual(calls, [
            "https://cn.pornhub.com/recommended",
            "https://cn.pornhub.com/recommended?page=2",
        ])
        self.assertEqual(result["creator_count"], 2)
        self.assertEqual(result["page_count"], 2)
        self.assertTrue(result["reached_limit"])

    async def test_personalization_cookie_is_opt_in_on_http_client(self):
        regular = scraper._client()
        recommended = scraper._client(personalized_recommendations=True)
        try:
            self.assertNotIn("cookieConsent", dict(regular.cookies.items()))
            self.assertEqual(
                dict(recommended.cookies.items()).get("cookieConsent"), "3")
        finally:
            await regular.aclose()
            await recommended.aclose()


class DiscoveryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = (Path(self.temp.name) / "discovery.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        # get_discovery 会安排非阻塞封面加热；测试也必须模拟应用 lifespan，
        # 先回收后台任务再释放临时数据库连接池。
        await main_module.shutdown_preview_warmups()
        main_module.library_index._invalidate_membership()
        await self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def result():
        performer = {
            "ph_id": "sample-model", "profile_type": "model",
            "profile_slug": "sample-model", "name": "Sample Model",
            "url": "https://cn.pornhub.com/model/sample-model",
        }
        channel = {
            "ph_id": "channel:sample-channel", "profile_type": "channel",
            "profile_slug": "sample-channel", "name": "Sample Channel",
            "url": "https://cn.pornhub.com/channels/sample-channel",
        }
        return {
            "source": "home", "label": "主页", "url": "https://cn.pornhub.com/",
            "items": [
                {"creator": performer, "viewkey": "aaa1111111111", "title": "One",
                 "url": "https://cn.pornhub.com/view_video.php?viewkey=aaa1111111111",
                 "thumbnail": "https://cdn.example/one.jpg", "duration": 60,
                 "source_order": 0},
                {"creator": performer, "viewkey": "bbb2222222222", "title": "Two",
                 "url": "https://cn.pornhub.com/view_video.php?viewkey=bbb2222222222",
                 "thumbnail": "https://cdn.example/two.jpg", "duration": 70,
                 "source_order": 1},
                {"creator": channel, "viewkey": "", "title": "", "url": channel["url"],
                 "thumbnail": "", "duration": 0, "source_order": 2},
            ],
        }

    async def test_snapshot_groups_repeated_creator_and_namespaces_channel(self):
        saved = await discovery.save_snapshot(
            self.result(), session_factory=self.session)
        async with self.session() as session:
            models = (await session.execute(
                select(Model).order_by(Model.id))).scalars().all()
            items = (await session.execute(
                select(DiscoveryItem).order_by(DiscoveryItem.source_order))).scalars().all()
        self.assertEqual(saved["creator_count"], 2)
        self.assertEqual([model.ph_id for model in models], [
            "sample-model", "channel:sample-channel",
        ])
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].model_id, items[1].model_id)

    async def test_read_api_groups_covers_and_falls_back_to_synced_channel_video(self):
        await discovery.save_snapshot(self.result(), session_factory=self.session)
        async with self.session() as session:
            channel = (await session.execute(select(Model).where(
                Model.profile_type == "channel"))).scalar_one()
            session.add(Video(
                model_id=channel.id, viewkey="ccc3333333333", title="Channel Video",
                url="https://cn.pornhub.com/view_video.php?viewkey=ccc3333333333",
                thumbnail="https://cdn.example/channel.jpg", duration=80,
            ))
            await session.commit()
        with patch_api("async_session", self.session):
            payload = await main_module.get_discovery("home")
        self.assertEqual(len(payload["creators"]), 2)
        performer, channel = payload["creators"]
        self.assertEqual([performer["source_rank"], channel["source_rank"]], [1, 2])
        self.assertEqual(len(performer["discovery_videos"]), 2)
        self.assertEqual(channel["profile_type"], "channel")
        self.assertEqual(
            channel["discovery_videos"][0]["thumbnail"],
            "https://cdn.example/channel.jpg",
        )

    async def test_channel_sync_error_never_falls_back_to_model_lookup(self):
        async with self.session() as session:
            channel = Model(
                ph_id="channel:sample", profile_type="channel", profile_slug="sample",
                name="Sample", url="https://cn.pornhub.com/channels/sample",
            )
            session.add(channel)
            await session.commit()
            channel_id = channel.id
        scrape = AsyncMock(side_effect=scraper.ScrapeError("channel failed"))
        lookup = AsyncMock()
        with patch.object(model_sync.scraper, "scrape_model_videos", new=scrape), \
                patch.object(model_sync.scraper, "lookup_model_profile", new=lookup):
            with self.assertRaisesRegex(scraper.ScrapeError, "channel failed"):
                await model_sync.sync_model_core(
                    channel_id, session_factory=self.session)
        lookup.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
