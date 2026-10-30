from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bs4 import BeautifulSoup
from bs4.element import Tag

from backend import scraper


BASE = "https://cn.pornhub.com"


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _response(html, path="/video"):
    return SimpleNamespace(text=html, url=BASE + path, status_code=200)


def _card(viewkey, slug):
    return (
        '<li class="pcVideoListItem">'
        f'<a href="/view_video.php?viewkey={viewkey}" title="{viewkey}">'
        f'<img data-src="https://cdn.example/{viewkey}.jpg"></a>'
        '<var class="duration">1:02</var><div class="videoUploaderBlock">'
        f'<a href="/model/{slug}">{slug}</a></div></li>'
    )


def _creator(slug, kind="model"):
    profile_type = {"channels": "channel", "users": "user"}.get(kind, kind)
    identity = slug if kind in {"model", "pornstar"} else f"{profile_type}:{slug}"
    return {
        "ph_id": identity, "profile_type": profile_type,
        "profile_slug": slug, "url": f"{BASE}/{kind}/{slug}", "name": slug,
    }


class ScraperParsingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings_patch = patch.object(scraper.settings, "ph_base_url", BASE)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    @contextmanager
    def worker_dom(self, expected_count=1):
        """同时验证 DOM 构造与选择器均不占用事件循环线程。"""
        event_thread = threading.get_ident()
        original_select = Tag.select
        original_select_one = Tag.select_one

        def construct(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), event_thread)
            return BeautifulSoup(*args, **kwargs)

        def select(node, *args, **kwargs):
            self.assertNotEqual(threading.get_ident(), event_thread)
            return original_select(node, *args, **kwargs)

        def select_one(node, *args, **kwargs):
            self.assertNotEqual(threading.get_ident(), event_thread)
            return original_select_one(node, *args, **kwargs)

        with patch.object(scraper, "BeautifulSoup", side_effect=construct) as constructor, \
                patch.object(Tag, "select", autospec=True, side_effect=select), \
                patch.object(Tag, "select_one", autospec=True, side_effect=select_one):
            yield
        self.assertEqual(constructor.call_count, expected_count)

    async def test_discovery_parsers_use_one_worker_dom_and_preserve_fields(self):
        shorties = {
            "vkey": "sample1", "profileUrl": "/model/alpha", "name": "alpha",
            "videoTitle": "sample1", "largePreviewUrl": "https://cdn.example/sample1.jpg",
        }
        regular = {
            "creator": _creator("alpha"), "viewkey": "sample1", "title": "sample1",
            "url": BASE + "/view_video.php?viewkey=sample1", "duration": 62,
            "thumbnail": "https://cdn.example/sample1.jpg", "source_order": 0,
        }
        cases = [
            ("hot", _response('<ul id="videoCategory">' + _card("sample1", "alpha")
                              + '</ul><aside>' + _card("outside", "other") + '</aside>'), regular),
            ("shorties", _response('<script>JSON_SHORTIES = insertAfterNthPosition('
                                    + json.dumps([shorties]) + ', 8);</script>', "/shorties"),
             {**regular, "duration": 0}),
            ("random", _response('<div class="userInfo"><a href="/model/alpha">alpha</a></div>'
                                  '<script>var flashvars_1 = {"video_title":"sample1",'
                                  '"video_duration":62,"image_url":"https://cdn.example/sample1.jpg"};'
                                  '</script>', "/view_video.php?viewkey=sample1"), regular),
            ("playlists", _response('<li class="full-width" id="playlist_1">'
                                     '<a class="usernameLink" href="/model/alpha">alpha</a>'
                                     '<a href="/view_video.php?viewkey=sample1">Play</a>'
                                     '<a class="viewPlaylistLink" href="/playlist/1">List</a>'
                                     '<span class="title">sample1</span>'
                                     '<img src="https://cdn.example/sample1.jpg"></li>'),
             {**regular, "duration": 0, "url": BASE + "/playlist/1"}),
            ("channels", _response('<ul id="allChannelsSection"><li class="mainChannels">'
                                    '<a href="/channels/alpha">alpha</a></li></ul>'),
             {"creator": _creator("alpha", "channels"), "viewkey": "", "title": "",
              "url": BASE + "/channels/alpha", "duration": 0, "thumbnail": "", "source_order": 0}),
            ("viewers_choice", _response('<div class="contestHub--viewersChoice">'
                                          + _card("sample1", "alpha") + '</div>'
                                          + _card("outside", "other")), regular),
        ]
        for source, response, expected in cases:
            with self.subTest(source=source), self.worker_dom(), \
                    patch.object(scraper, "_client", return_value=_Client()), \
                    patch.object(scraper, "_fetch_ok", new=AsyncMock(return_value=response)) as fetch:
                result = await scraper.scrape_discovery(source, 1)
                self.assertEqual(result["items"], [expected])
                self.assertEqual(result["creator_count"], 1)
                self.assertEqual(result["page_count"], 1)
                self.assertTrue(result["reached_limit"])
                self.assertEqual(fetch.await_count, 1)

    async def test_discovery_limit_keeps_existing_and_first_new_creators_in_source_order(self):
        responses = [
            _response('<ul id="videoCategory">' + _card("a1", "alpha") + '</ul>'),
            _response('<ul id="videoCategory">' + ''.join(
                _card(key, slug) for key, slug in [
                    ("b1", "beta"), ("a2", "alpha"), ("b2", "beta"),
                    ("c1", "gamma"), ("a3", "alpha"), ("b3", "beta"),
                ]) + '</ul>'),
        ]
        with patch.object(scraper, "_client", return_value=_Client()), \
                patch.object(scraper, "_fetch_ok", new=AsyncMock(side_effect=responses)), \
                patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            result = await scraper.scrape_discovery("hot", 2)
        self.assertEqual([item["viewkey"] for item in result["items"]],
                         ["a1", "b1", "a2", "b2", "a3", "b3"])
        self.assertEqual([item["source_order"] for item in result["items"]], list(range(6)))
        self.assertEqual(result["creator_count"], 2)

    async def test_discovery_parser_errors_keep_original_type_and_message(self):
        cases = [
            ("shorties", "<html></html>", "短片页缺少 JSON_SHORTIES 数据，PH 页面结构可能已变化"),
            ("shorties", "<script>JSON_SHORTIES = insertAfterNthPosition(invalid, 8);</script>",
             "短片页内嵌数据无法解析"),
            ("random", "<html></html>", "随机视频页缺少可验证的上传者或视频数据"),
        ]
        for source, html, message in cases:
            with self.subTest(source=source, message=message), \
                    patch.object(scraper, "_client", return_value=_Client()), \
                    patch.object(scraper, "_fetch_ok", new=AsyncMock(return_value=_response(html))) as fetch:
                with self.assertRaises(scraper.ScrapeError) as raised:
                    await scraper.scrape_discovery(source)
                self.assertIs(type(raised.exception), scraper.ScrapeError)
                self.assertEqual(str(raised.exception), message)
                self.assertEqual(fetch.await_count, 1)

    async def test_home_next_link_selection_runs_in_worker_and_rejects_external_link(self):
        response = _response('<link rel="next" href="https://example.com/video?page=2">'
                             + _card("a1", "alpha"), "/")
        with self.worker_dom(), patch.object(scraper, "_client", return_value=_Client()), \
                patch.object(scraper, "_fetch_ok", new=AsyncMock(return_value=response)) as fetch:
            result = await scraper.scrape_discovery("home", 2)
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(fetch.await_count, 1)

    async def test_trusted_source_worker_preserves_scope_and_limit_order(self):
        response = _response('<div class="headerSubMenu">' + _card("outside", "other")
                             + '</div><ul>' + ''.join(_card(key, slug) for key, slug in [
                                 ("a1", "alpha"), ("b1", "beta"), ("a2", "alpha"),
                             ]) + '</ul>', "/channels/source/videos")
        with self.worker_dom(), patch.object(scraper, "_client", return_value=_Client()), \
                patch.object(scraper, "_fetch_ok", new=AsyncMock(return_value=response)) as fetch:
            result = await scraper.scrape_trusted_source_url(response.url, max_creators=1)
        self.assertEqual([item["viewkey"] for item in result["items"]], ["a1", "a2"])
        self.assertEqual([item["source_order"] for item in result["items"]], [0, 1])
        self.assertEqual(result["creator_count"], 1)
        self.assertEqual(fetch.await_count, 1)

    async def test_ranking_worker_preserves_primary_list_and_duplicate_page_progress(self):
        def ranking_card(slug):
            return (f'<li class="pornstarLi"><a href="/model/{slug}">{slug}</a>'
                    f'<span class="pornStarName">{slug}</span></li>')

        responses = [_response('<ul class="pornstarsList">' + ranking_card(slug)
                               + '</ul><ul class="pornstarsSort">'
                               + ranking_card("outside") + '</ul>')
                     for slug in ("alpha", "alpha", "beta")]
        with self.worker_dom(3), patch.object(scraper, "_client", return_value=_Client()), \
                patch.object(scraper, "_fetch_ok", new=AsyncMock(side_effect=responses)) as fetch, \
                patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            result = await scraper.scrape_ranking(BASE + "/pornstars", 2)
        self.assertEqual([item["ph_id"] for item in result], ["alpha", "beta"])
        self.assertEqual([item["scrape_order"] for item in result], [0, 1])
        self.assertEqual(fetch.await_count, 3)
        self.assertIn("gender=female", fetch.await_args_list[0].args[1])

    async def test_discover_ranking_stops_after_two_duplicate_pages(self):
        response = _response('<a href="/model/alpha">Alpha</a>'
                             '<a href="/channels/other">Other</a>')
        with self.worker_dom(3), patch.object(scraper, "_client", return_value=_Client()), \
                patch.object(scraper, "_fetch_ok", new=AsyncMock(return_value=response)) as fetch, \
                patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            result = await scraper.scrape_ranking(BASE + "/user/discover/models", 2)
        self.assertEqual(result, [{
            "ph_id": "alpha", "name": "Alpha", "url": BASE + "/model/alpha",
            "thumbnail": "", "scrape_order": 0,
        }])
        self.assertEqual(fetch.await_count, 3)

    async def test_profile_lookup_and_known_url_run_dom_parsing_in_worker(self):
        profile = _response('<section class="topProfileHeader"><h1>Alpha Name</h1>'
                            '<img id="getAvatar" src="https://cdn.example/avatar.jpg">'
                            '</section>', "/pornstar/alpha")
        expected = {
            "ph_id": "alpha", "name": "Alpha Name", "url": BASE + "/pornstar/alpha",
            "thumbnail": "https://cdn.example/avatar.jpg", "rank": 0,
            "ph_video_count": 0, "views_text": "",
        }
        with self.worker_dom(2), patch.object(scraper, "_client", return_value=_Client()), \
                patch.object(scraper, "_fetch_ok", new=AsyncMock(side_effect=[
                    _response("<html>directory fallback</html>", "/model/alpha"), profile,
                ])) as fetch:
            self.assertEqual(await scraper.lookup_model_profile("Alpha"), expected)
        self.assertEqual(fetch.await_count, 2)
        with self.worker_dom(), \
                patch.object(scraper, "_fetch_ok", new=AsyncMock(return_value=profile)) as fetch:
            self.assertEqual(await scraper.fetch_model_profile_url(
                BASE + "/model/alpha", client=object()), expected)
        self.assertEqual(fetch.await_count, 1)


if __name__ == "__main__":
    unittest.main()
