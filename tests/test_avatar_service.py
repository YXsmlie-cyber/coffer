from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import ANY, AsyncMock, patch

from backend import avatar_service, avatar_store


class AvatarPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await avatar_service.close_clients()

    async def asyncTearDown(self):
        await avatar_service.close_clients()

    @staticmethod
    def item(thumbnail: str):
        return {
            "ph_id": "sample-model",
            "name": "Sample Model",
            "url": "https://cn.pornhub.com/model/sample-model",
            "thumbnail": thumbnail,
            "rank": 5,
        }

    async def test_standard_ranking_avatar_is_cached_without_second_profile_request(self):
        cache = AsyncMock(return_value=avatar_store.AvatarAsset(
            source_url="https://ei.phncdn.com/avatar.jpg",
            sha256="d" * 64,
            byte_size=1024,
        ))
        profile = AsyncMock()
        with patch.object(avatar_service.avatar_store, "cache_avatar", new=cache), \
                patch.object(avatar_service.scraper, "fetch_model_profile_url", new=profile):
            prepared = await avatar_service._prepare_one(
                self.item("https://ei.phncdn.com/avatar.jpg"), object(), object())
        profile.assert_not_awaited()
        self.assertEqual(prepared["avatar_sha256"], "d" * 64)
        self.assertEqual(prepared["rank"], 5)

    async def test_discover_identity_requires_authoritative_profile_avatar(self):
        profile = AsyncMock(return_value={
            "ph_id": "sample-model",
            "name": "Sample Model Canonical",
            "url": "https://cn.pornhub.com/model/sample-model",
            "thumbnail": "https://ei.phncdn.com/fresh.jpg",
        })
        cache = AsyncMock(return_value=avatar_store.AvatarAsset(
            source_url="https://ei.phncdn.com/fresh.jpg",
            sha256="e" * 64,
            byte_size=1024,
        ))
        with patch.object(avatar_service.scraper, "fetch_model_profile_url", new=profile), \
                patch.object(avatar_service.avatar_store, "cache_avatar", new=cache):
            prepared = await avatar_service._prepare_one(
                self.item(""), object(), object())
        profile.assert_awaited_once()
        cache.assert_awaited_once_with(
            "https://ei.phncdn.com/fresh.jpg", client=ANY,
            referer="https://cn.pornhub.com/model/sample-model")
        self.assertEqual(prepared["name"], "Sample Model Canonical")
        self.assertEqual(prepared["avatar_sha256"], "e" * 64)

    async def test_recent_verified_asset_skips_all_network_clients(self):
        existing = {
            "sample-model": {
                "avatar_status": "ready",
                "avatar_sha256": "f" * 64,
                "avatar_source_url": "https://cdn.example/existing.jpg",
                "avatar_checked_at": datetime.now(),
                "avatar_updated_at": datetime.now(),
            },
        }
        with (
            patch.object(avatar_service.avatar_store, "avatar_path",
                         return_value=Path("existing.webp")),
            patch.object(avatar_service, "_shared_clients", new=AsyncMock()) as clients,
            patch.object(avatar_service, "_prepare_one", new=AsyncMock()) as prepare,
        ):
            prepared, failures = await avatar_service.prepare_models(
                [self.item("")], existing_assets=existing)
        self.assertEqual(failures, [])
        self.assertEqual(prepared[0]["avatar_sha256"], "f" * 64)
        clients.assert_not_awaited()
        prepare.assert_not_awaited()

    async def test_profile_and_image_clients_are_reused_across_repair_batches(self):
        class FakeClient:
            def __init__(self):
                self.is_closed = False

            async def aclose(self):
                self.is_closed = True

        profile_client = FakeClient()
        image_client = FakeClient()
        cache = AsyncMock(return_value=avatar_store.AvatarAsset(
            source_url="https://ei.phncdn.com/avatar.jpg",
            sha256="a" * 64,
            byte_size=1024,
        ))
        with (
            patch.object(avatar_service.scraper, "_client",
                         return_value=profile_client) as profile_factory,
            patch.object(avatar_service.avatar_store, "create_client",
                         return_value=image_client) as image_factory,
            patch.object(avatar_service.avatar_store, "cache_avatar", new=cache),
        ):
            first, _ = await avatar_service.prepare_models([
                self.item("https://ei.phncdn.com/avatar.jpg")])
            second, _ = await avatar_service.prepare_models([
                {**self.item("https://ei.phncdn.com/avatar2.jpg"),
                 "ph_id": "sample-model-2"}])
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            profile_factory.assert_called_once_with()
            image_factory.assert_called_once_with()
            self.assertIs(cache.await_args_list[0].kwargs["client"], image_client)
            self.assertIs(cache.await_args_list[1].kwargs["client"], image_client)
            await avatar_service.close_clients()
        self.assertTrue(profile_client.is_closed)
        self.assertTrue(image_client.is_closed)


if __name__ == "__main__":
    unittest.main()
