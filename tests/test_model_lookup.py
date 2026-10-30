import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import scraper
from backend.database import Base, Model, Task
from backend.model_sync_coordinator import ModelSyncCoordinator
import backend.main as main_module
from api_patch import patch_api
from backend.main import ModelLookupRequest


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


def _profile_response(kind: str, slug: str, name: str = "MyPornPlugg"):
    return SimpleNamespace(
        url=f"https://cn.pornhub.com/{kind}/{slug}",
        text=(
            "<html><body><section class='topProfileHeader'>"
            "<div class='nameSubscribe'><h1>" + name + "</h1></div>"
            "<img id='getAvatar' data-src='https://ei.phncdn.com/pics/users/avatar.jpg'>"
            "</section></body></html>"
        ),
    )


def _directory_response(kind: str, slug: str):
    return SimpleNamespace(
        url=f"https://cn.pornhub.com/{kind}/{slug}",
        text="<html><body><h1>最热门的色情明星</h1></body></html>",
    )


class ModelProfileLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_playlist_has_a_separate_small_retry_budget(self):
        parent = scraper._FetchBudget(retries=5, timeout_seconds=60)
        parent_retries = parent.retries_remaining
        fetch = AsyncMock(return_value=None)

        with patch.object(scraper, "_fetch_with_budget", fetch):
            result = await scraper._fetch_public_playlist_videos(
                object(),
                "https://cn.pornhub.com/view_video.php?viewkey=abcdef1234567",
                "verified",
                budget=parent,
            )

        self.assertEqual(result, [])
        child = fetch.await_args.kwargs["budget"]
        self.assertIsNot(child, parent)
        self.assertEqual(child.retries_remaining, 1)
        self.assertEqual(parent.retries_remaining, parent_retries)

    async def test_model_video_page_unavailable_shell_is_not_retried_as_dom_drift(self):
        unavailable = SimpleNamespace(
            url="https://cn.pornhub.com/model/removed/videos",
            text="<html><body><div class='geoBlocked'><h1>乱码也不影响</h1></div></body></html>",
        )

        async def fetch(_client, _url, *, page_validator=None):
            self.assertIsNotNone(page_validator)
            return unavailable if page_validator(unavailable) else None

        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"), \
                patch.object(scraper, "_client", return_value=_FakeClient()), \
                patch.object(scraper, "_fetch_ok", new=fetch):
            with self.assertRaises(scraper.ModelProfileUnavailable):
                await scraper.scrape_model_videos(
                    "https://cn.pornhub.com/model/removed")

    async def test_model_video_first_page_requires_positive_profile_boundary(self):
        valid = SimpleNamespace(
            url="https://cn.pornhub.com/model/verified/videos",
            text=(
                "<div class='profileVids'><ul id='mostRecentVideosSection'>"
                "<li class='pcVideoListItem'>"
                "<a href='/view_video.php?viewkey=abcdef1234567' title='Video'></a>"
                "<var class='duration'>1:00</var>"
                "</li></ul></div>"
            ),
        )
        soft_block = SimpleNamespace(
            url=valid.url,
            text="<html><body><ul id='hottestMenuSection'></ul></body></html>",
        )

        async def fetch(_client, _url, *, page_validator=None):
            self.assertIsNotNone(page_validator)
            self.assertFalse(page_validator(soft_block))
            self.assertTrue(page_validator(valid))
            return valid

        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"), \
                patch.object(scraper, "_client", return_value=_FakeClient()), \
                patch.object(scraper, "_fetch_ok", new=fetch), \
                patch.object(scraper, "_fetch_public_playlist_videos",
                             new=AsyncMock(return_value=[])):
            videos = await scraper.scrape_model_videos(
                "https://cn.pornhub.com/model/verified", max_videos=1)

        self.assertEqual([item["viewkey"] for item in videos], ["abcdef1234567"])

    async def test_model_video_later_page_404_only_ends_pagination(self):
        first_page = SimpleNamespace(
            status_code=200,
            url="https://cn.pornhub.com/model/verified/videos",
            text=(
                "<div class='profileVids'><ul id='mostRecentVideosSection'>"
                "<li class='pcVideoListItem'>"
                "<a href='/view_video.php?viewkey=abcdef1234567' title='Video'></a>"
                "<var class='duration'>1:00</var>"
                "</li></ul></div>"
            ),
        )
        past_end = SimpleNamespace(
            status_code=404,
            url="https://cn.pornhub.com/model/verified/videos?page=2",
            text="not found",
        )

        async def fetch(_client, url, *, page_validator=None):
            if "page=2" in url:
                self.assertIsNone(page_validator)
                return past_end
            self.assertIsNotNone(page_validator)
            self.assertTrue(page_validator(first_page))
            return first_page

        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"), \
                patch.object(scraper, "_client", return_value=_FakeClient()), \
                patch.object(scraper, "_fetch_ok", new=fetch), \
                patch.object(scraper, "_fetch_public_playlist_videos",
                             new=AsyncMock(return_value=[])), \
                patch.object(scraper.asyncio, "sleep", new=AsyncMock()):
            videos = await scraper.scrape_model_videos(
                "https://cn.pornhub.com/model/verified")

        self.assertEqual([item["viewkey"] for item in videos], ["abcdef1234567"])

    async def test_model_video_later_page_budget_failure_never_returns_partial(self):
        first_page = SimpleNamespace(
            status_code=200,
            url="https://cn.pornhub.com/model/verified/videos",
            text=(
                "<div class='profileVids'><ul id='mostRecentVideosSection'>"
                "<li class='pcVideoListItem'>"
                "<a href='/view_video.php?viewkey=abcdef1234567' title='Video'></a>"
                "<var class='duration'>1:00</var>"
                "</li></ul></div>"
            ),
        )

        async def fetch(_client, url, *, page_validator=None):
            if "page=2" in url:
                return None
            self.assertTrue(page_validator(first_page))
            return first_page

        with (
            patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"),
            patch.object(scraper, "_client", return_value=_FakeClient()),
            patch.object(scraper, "_fetch_ok", new=fetch),
            patch.object(scraper, "_fetch_public_playlist_videos",
                         new=AsyncMock(return_value=[])),
            patch.object(scraper.asyncio, "sleep", new=AsyncMock()),
        ):
            with self.assertRaises(scraper.ScrapeFetchError):
                await scraper.scrape_model_videos(
                    "https://cn.pornhub.com/model/verified")

    async def test_model_video_later_200_shell_never_returns_partial(self):
        first_page = SimpleNamespace(
            status_code=200,
            url="https://cn.pornhub.com/model/verified/videos",
            text=(
                "<div class='profileVids'><ul id='mostRecentVideosSection'>"
                "<li class='pcVideoListItem'>"
                "<a href='/view_video.php?viewkey=abcdef1234567' title='Video'></a>"
                "</li></ul></div>"
            ),
        )
        second_shell = SimpleNamespace(
            status_code=200,
            url="https://cn.pornhub.com/model/verified/videos?page=2",
            text="<html><body><div class='challenge'>please wait</div></body></html>",
        )

        async def fetch(_client, url, *, page_validator=None):
            if "page=2" in url:
                return second_shell
            self.assertTrue(page_validator(first_page))
            return first_page

        with (
            patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"),
            patch.object(scraper, "_client", return_value=_FakeClient()),
            patch.object(scraper, "_fetch_ok", new=fetch),
            patch.object(scraper, "_fetch_public_playlist_videos",
                         new=AsyncMock(return_value=[])),
            patch.object(scraper.asyncio, "sleep", new=AsyncMock()),
        ):
            with self.assertRaises(scraper.ScrapeFetchError):
                await scraper.scrape_model_videos(
                    "https://cn.pornhub.com/model/verified")

    async def test_removed_account_redirect_to_directory_is_unavailable_at_once(self):
        """已删除账号被 301 到 /pornstars：一次就判定，不能当限流重试到超时。"""
        directory = SimpleNamespace(
            status_code=200,
            url="https://cn.pornhub.com/pornstars",
            history=[SimpleNamespace(status_code=301)],
            text="<html><body><ul id='popularPornstars'></ul></body></html>",
        )
        not_redirected = SimpleNamespace(status_code=200, url=directory.url,
                                         history=[], text=directory.text)
        calls = 0

        async def fetch(_client, _url, *, page_validator=None):
            nonlocal calls
            calls += 1
            self.assertFalse(page_validator(not_redirected))
            return directory if page_validator(directory) else None

        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"), \
                patch.object(scraper, "_client", return_value=_FakeClient()), \
                patch.object(scraper, "_fetch_ok", new=fetch):
            with self.assertRaisesRegex(scraper.ModelProfileUnavailable, "已删除"):
                await scraper.scrape_model_videos("https://cn.pornhub.com/model/gone")
        self.assertEqual(calls, 1)

    async def test_profile_with_site_empty_state_is_unavailable_not_retried(self):
        pages = {
            "https://cn.pornhub.com/model/emptied": (
                "https://cn.pornhub.com/model/emptied/videos",
                "<div class='profileVids'><div class='nf-wrapper'></div>"
                "<div class='emptyBoxWrapper'><p>没有视频……</p></div></div>"
                "<ul id='hottestMenuSection'><li class='pcVideoListItem'>"
                "<a href='/view_video.php?viewkey=dropdown12345'></a></li></ul>",
            ),
            "https://cn.pornhub.com/pornstar/no-uploads": (
                "https://cn.pornhub.com/pornstar/no-uploads/videos/upload",
                "<div class='profileVids'><div class='reset'></div>"
                "<p class='empty'>没有视频……</p></div>",
            ),
        }
        for model_url, (final_url, html) in pages.items():
            with self.subTest(model_url=model_url):
                response = SimpleNamespace(status_code=200, url=final_url, text=html)

                async def fetch(_client, _url, *, page_validator=None, response=response):
                    return response if page_validator(response) else None

                with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"), \
                        patch.object(scraper, "_client", return_value=_FakeClient()), \
                        patch.object(scraper, "_fetch_ok", new=fetch):
                    with self.assertRaisesRegex(scraper.ModelProfileUnavailable, "没有公开视频"):
                        await scraper.scrape_model_videos(model_url)

    @staticmethod
    def _pornstar_page(url: str, items: list[str], listed_pages: list[int]):
        cards = "".join(
            "<li class='pcVideoListItem'><div class='usernameWrap'>"
            "<a href='/pornstar/listed'>Listed</a></div>"
            f"<a href='/view_video.php?viewkey={key}' title='{key}'></a></li>"
            for key in items)
        pagination = "".join(
            f"<li class='page_number'><a href='?page={number}'>{number}</a></li>"
            for number in listed_pages)
        return SimpleNamespace(
            status_code=200,
            url=url,
            text=(
                "<div class='profileVids'><div class='videoUList'>"
                f"<ul id='moreData'>{cards}</ul></div></div>"
                f"<div class='pagination3'><ul>{pagination}"
                "<li class='page_next disabled'><a href=''></a></li></ul></div>"
            ),
        )

    async def test_pornstar_empty_list_past_last_page_ends_pagination(self):
        """PH 对越界页返回 200 + 空 #moreData，分页只列到上一页：这是到底，不是 DOM 漂移。"""
        base = "https://cn.pornhub.com/pornstar/listed/videos/upload"
        first = self._pornstar_page(base, ["firstkey00001", "firstkey00002"], [1, 2])
        second = self._pornstar_page(f"{base}?page=2", ["secondkey0001"], [1, 2])
        past_end = self._pornstar_page(f"{base}?page=3", [], [1, 2])

        async def fetch(_client, url, *, page_validator=None):
            if "page=3" in url:
                return past_end
            if "page=2" in url:
                return second
            self.assertTrue(page_validator(first))
            return first

        with (
            patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"),
            patch.object(scraper, "_client", return_value=_FakeClient()),
            patch.object(scraper, "_fetch_ok", new=fetch),
            patch.object(scraper, "_fetch_public_playlist_videos",
                         new=AsyncMock(return_value=[])),
            patch.object(scraper.asyncio, "sleep", new=AsyncMock()),
        ):
            videos = await scraper.scrape_model_videos(
                "https://cn.pornhub.com/pornstar/listed")

        self.assertEqual([item["viewkey"] for item in videos],
                         ["firstkey00001", "firstkey00002", "secondkey0001"])

    async def test_pornstar_empty_list_on_a_listed_page_still_refuses_partial(self):
        base = "https://cn.pornhub.com/pornstar/listed/videos/upload"
        first = self._pornstar_page(base, ["firstkey00001"], [1, 2, 3])
        # 分页仍列出第 2、3 页，却给了空容器：可能是半截页面，不能当成到底。
        hollow = self._pornstar_page(f"{base}?page=2", [], [1, 2, 3])

        async def fetch(_client, url, *, page_validator=None):
            if "page=2" in url:
                return hollow
            self.assertTrue(page_validator(first))
            return first

        with (
            patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"),
            patch.object(scraper, "_client", return_value=_FakeClient()),
            patch.object(scraper, "_fetch_ok", new=fetch),
            patch.object(scraper, "_fetch_public_playlist_videos",
                         new=AsyncMock(return_value=[])),
            patch.object(scraper.asyncio, "sleep", new=AsyncMock()),
        ):
            with self.assertRaises(scraper.ScrapeFetchError):
                await scraper.scrape_model_videos(
                    "https://cn.pornhub.com/pornstar/listed")

    async def test_channel_later_page_budget_failure_never_returns_partial(self):
        first_page = SimpleNamespace(
            status_code=200,
            url="https://cn.pornhub.com/channels/verified/videos",
            text=(
                "<ul id='showAllChanelVideos'><li class='pcVideoListItem'>"
                "<a href='/view_video.php?viewkey=channel1234567' title='Video'></a>"
                "</li></ul>"
            ),
        )

        async def fetch(_client, url, *, page_validator=None):
            if "page=2" in url:
                return None
            self.assertTrue(page_validator(first_page))
            return first_page

        with (
            patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"),
            patch.object(scraper, "_client", return_value=_FakeClient()),
            patch.object(scraper, "_fetch_ok", new=fetch),
            patch.object(scraper.asyncio, "sleep", new=AsyncMock()),
        ):
            with self.assertRaises(scraper.ScrapeFetchError):
                await scraper.scrape_model_videos(
                    "https://cn.pornhub.com/channels/verified")

    async def test_channel_later_200_shell_never_returns_partial(self):
        first_page = SimpleNamespace(
            status_code=200,
            url="https://cn.pornhub.com/channels/verified/videos",
            text=(
                "<ul id='showAllChanelVideos'><li class='pcVideoListItem'>"
                "<a href='/view_video.php?viewkey=channel1234567' title='Video'></a>"
                "</li></ul>"
            ),
        )
        shell = SimpleNamespace(
            status_code=200,
            url="https://cn.pornhub.com/channels/verified/videos?page=2",
            text="<html><body>unknown shell</body></html>",
        )

        async def fetch(_client, url, *, page_validator=None):
            if "page=2" in url:
                return shell
            self.assertTrue(page_validator(first_page))
            return first_page

        with (
            patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"),
            patch.object(scraper, "_client", return_value=_FakeClient()),
            patch.object(scraper, "_fetch_ok", new=fetch),
            patch.object(scraper.asyncio, "sleep", new=AsyncMock()),
        ):
            with self.assertRaises(scraper.ScrapeFetchError):
                await scraper.scrape_model_videos(
                    "https://cn.pornhub.com/channels/verified")

    async def test_model_profile_is_preferred_and_strictly_parsed(self):
        fetch = AsyncMock(return_value=_profile_response("model", "mypornplugg"))
        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"), \
                patch.object(scraper, "_client", return_value=_FakeClient()), \
                patch.object(scraper, "_fetch_ok", new=fetch):
            result = await scraper.lookup_model_profile("@MyPornPlugg")

        self.assertEqual(result["ph_id"], "mypornplugg")
        self.assertEqual(result["name"], "MyPornPlugg")
        self.assertEqual(result["rank"], 0)
        self.assertIn("/model/mypornplugg", result["url"])
        self.assertEqual(fetch.await_count, 1)

    async def test_pornstar_profile_is_used_after_model_directory_fallback(self):
        fetch = AsyncMock(side_effect=[
            _directory_response("model", "riley-reid"),
            _profile_response("pornstar", "riley-reid", "Riley Reid"),
        ])
        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"), \
                patch.object(scraper, "_client", return_value=_FakeClient()), \
                patch.object(scraper, "_fetch_ok", new=fetch):
            result = await scraper.lookup_model_profile("Riley Reid")

        self.assertEqual(result["name"], "Riley Reid")
        self.assertIn("/pornstar/riley-reid", result["url"])
        self.assertEqual(fetch.await_count, 2)

    async def test_directory_fallback_pages_are_not_accepted_as_profiles(self):
        fetch = AsyncMock(side_effect=[
            _directory_response("model", "missing-name"),
            _directory_response("pornstar", "missing-name"),
        ])
        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"), \
                patch.object(scraper, "_client", return_value=_FakeClient()), \
                patch.object(scraper, "_fetch_ok", new=fetch):
            with self.assertRaises(scraper.ModelLookupNotFound):
                await scraper.lookup_model_profile("missing-name")

    async def test_unreachable_candidate_returns_retryable_error(self):
        fetch = AsyncMock(side_effect=[None, _directory_response("pornstar", "blocked")])
        with patch.object(scraper.settings, "ph_base_url", "https://cn.pornhub.com"), \
                patch.object(scraper, "_client", return_value=_FakeClient()), \
                patch.object(scraper, "_fetch_ok", new=fetch):
            with self.assertRaises(scraper.ModelLookupUnavailable):
                await scraper.lookup_model_profile("blocked")

    def test_arbitrary_url_is_rejected_as_query(self):
        with self.assertRaises(scraper.InvalidModelQuery):
            scraper.normalize_model_query("https://example.com/model/name")

    async def test_known_profile_url_accepts_same_slug_kind_redirect(self):
        response = _profile_response("pornstar", "same-person", "Same Person")
        fetch = AsyncMock(return_value=response)
        with patch.object(scraper, "_fetch_ok", new=fetch):
            result = await scraper.fetch_model_profile_url(
                "https://cn.pornhub.com/model/same-person", client=object())
        self.assertEqual(result["ph_id"], "same-person")
        self.assertIn("/pornstar/same-person", result["url"])

    async def test_known_profile_url_rejects_identity_changing_redirect(self):
        response = _profile_response("model", "other-person", "Other Person")
        with patch.object(scraper, "_fetch_ok", new=AsyncMock(return_value=response)):
            with self.assertRaises(scraper.ModelLookupUnavailable):
                await scraper.fetch_model_profile_url(
                    "https://cn.pornhub.com/model/same-person", client=object())


class ModelLookupEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.avatar_dir = Path(self.temp.name) / "avatars"
        self.avatar_dir.mkdir()
        db_path = (Path(self.temp.name) / "lookup.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_patch = patch_api("async_session", self.session)
        self.coordinator_patch = patch_api(
            "model_sync_coordinator",
            ModelSyncCoordinator(session_factory=self.session),
        )
        self.lock_patch = patch_api("_model_lookup_lock", asyncio.Lock())
        self.avatar_dir_patch = patch.object(
            main_module.avatar_store.settings, "avatar_dir", str(self.avatar_dir))
        self.avatar_cache_patch = patch.object(
            main_module.avatar_store, "cache_avatar", new=AsyncMock(
                side_effect=self._cache_avatar))
        self.session_patch.start()
        self.coordinator_patch.start()
        self.lock_patch.start()
        self.avatar_dir_patch.start()
        self.avatar_cache_patch.start()

    async def asyncTearDown(self):
        self.avatar_cache_patch.stop()
        self.avatar_dir_patch.stop()
        self.lock_patch.stop()
        self.coordinator_patch.stop()
        self.session_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def profile(slug="mypornplugg", name="MyPornPlugg"):
        return {
            "ph_id": slug,
            "name": name,
            "url": f"https://cn.pornhub.com/model/{slug}",
            "thumbnail": "https://ei.phncdn.com/avatar.jpg",
            "rank": 0,
            "ph_video_count": 0,
            "views_text": "",
        }

    async def _cache_avatar(self, source_url, **_kwargs):
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        (self.avatar_dir / f"{digest}.webp").write_bytes(b"valid-test-avatar")
        return main_module.avatar_store.AvatarAsset(
            source_url=source_url, sha256=digest, byte_size=17)

    async def test_local_exact_name_hit_does_not_access_external_site(self):
        async with self.session() as session:
            model = Model(
                ph_id="local-name", name="Local Name",
                url="https://cn.pornhub.com/model/local-name", rank=17,
                avatar_sha256="a" * 64,
            )
            session.add(model)
            await session.commit()
            model_id = model.id
        (self.avatar_dir / f"{'a' * 64}.webp").write_bytes(b"local-avatar")

        external = AsyncMock()
        with patch.object(scraper, "lookup_model_profile", new=external):
            result = await main_module.lookup_model(ModelLookupRequest(query="LOCAL NAME"))

        self.assertEqual(result["model"]["id"], model_id)
        self.assertFalse(result["created"])
        external.assert_not_awaited()

    async def test_concurrent_repeated_lookup_creates_only_one_model(self):
        async def find_profile(_query):
            await asyncio.sleep(0)
            return self.profile()

        external = AsyncMock(side_effect=find_profile)
        with patch.object(scraper, "lookup_model_profile", new=external):
            first, second = await asyncio.gather(
                main_module.lookup_model(ModelLookupRequest(query="mypornplugg")),
                main_module.lookup_model(ModelLookupRequest(query="MyPornPlugg")),
            )

        async with self.session() as session:
            count = (await session.execute(select(func.count(Model.id)).where(
                Model.ph_id == "mypornplugg",
            ))).scalar_one()
        self.assertEqual(count, 1)
        self.assertEqual(first["model"]["id"], second["model"]["id"])
        self.assertEqual({first["created"], second["created"]}, {True, False})
        self.assertEqual(external.await_count, 1)

    async def test_locked_recheck_rejects_model_added_to_library_while_waiting(self):
        async with self.session() as session:
            model = Model(
                ph_id="library-race",
                name="Library Race",
                url="https://cn.pornhub.com/model/library-race",
                rank=0,
                avatar_sha256="b" * 64,
            )
            session.add(model)
            await session.commit()
            await session.refresh(model)
            model_id = model.id
        (self.avatar_dir / f"{'b' * 64}.webp").write_bytes(b"local-avatar")

        outside_membership = SimpleNamespace(
            contains=lambda _model: False,
            folders_by_model={},
        )
        folder = {"cid": "115-library-race", "name": "Library Race"}
        locked_membership = SimpleNamespace(
            contains=lambda candidate: candidate.id == model_id,
            folders_by_model={model_id: folder},
        )
        external = AsyncMock()
        with patch_api(
                "_find_exact_model",
                new=AsyncMock(side_effect=[None, model])), patch.object(
                main_module.library_index, "membership",
                new=AsyncMock(side_effect=[
                    outside_membership,
                    locked_membership,
                ])), patch.object(
                scraper, "lookup_model_profile", new=external):
            with self.assertRaises(HTTPException) as raised:
                await main_module.lookup_model(
                    ModelLookupRequest(query="Library Race"))

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "already_in_library")
        self.assertEqual(raised.exception.detail["folder"], folder)
        external.assert_not_awaited()

    async def test_lookup_errors_map_to_public_status_codes(self):
        cases = [
            (scraper.ModelLookupNotFound("不存在"), 404),
            (scraper.ModelLookupUnavailable("暂时不可用"), 502),
        ]
        for error, status in cases:
            with self.subTest(status=status), patch.object(
                    scraper, "lookup_model_profile", new=AsyncMock(side_effect=error)):
                with self.assertRaises(HTTPException) as raised:
                    await main_module.lookup_model(ModelLookupRequest(query=f"name-{status}"))
                self.assertEqual(raised.exception.status_code, status)

        with self.assertRaises(HTTPException) as raised:
            await main_module.lookup_model(ModelLookupRequest(query="   "))
        self.assertEqual(raised.exception.status_code, 422)

    async def test_sync_failure_keeps_unsynced_card_and_creates_no_tasks(self):
        with patch.object(scraper, "lookup_model_profile", new=AsyncMock(
                return_value=self.profile())):
            result = await main_module.lookup_model(ModelLookupRequest(query="mypornplugg"))
        model_id = result["model"]["id"]

        with patch.object(scraper, "scrape_model_videos", new=AsyncMock(
                side_effect=scraper.ScrapeError("blocked"))):
            with self.assertRaises(HTTPException) as raised:
                await main_module.sync_model(model_id)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail, "blocked")

        async with self.session() as session:
            model = (await session.execute(select(Model).where(
                Model.id == model_id,
            ))).scalar_one()
            task_count = (await session.execute(select(func.count(Task.id)))).scalar_one()
        self.assertIsNone(model.synced_at)
        self.assertEqual(model.name, "MyPornPlugg")
        self.assertEqual(task_count, 0)

    async def test_sync_unavailable_maps_to_gone_instead_of_server_error(self):
        with patch_api("_sync_model_core", new=AsyncMock(
                return_value={
                    "model_id": 7,
                    "status": "source_unavailable",
                    "error": "主页不可用或已删除",
                })):
            with self.assertRaises(HTTPException) as raised:
                await main_module.sync_model(7)

        self.assertEqual(raised.exception.status_code, 410)


if __name__ == "__main__":
    unittest.main()
