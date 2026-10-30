from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, Model
import backend.main as main_module
from api_patch import patch_api
from backend.main import RankingRequest


class AvatarPipelineEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.avatar_dir = root / "avatars"
        self.avatar_dir.mkdir()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{(root / 'pipeline.db').as_posix()}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_patch = patch_api("async_session", self.session)
        self.avatar_dir_patch = patch.object(
            main_module.avatar_store.settings, "avatar_dir", str(self.avatar_dir))
        self.session_patch.start()
        self.avatar_dir_patch.start()

    async def asyncTearDown(self):
        self.avatar_dir_patch.stop()
        self.session_patch.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def raw_model():
        return {
            "ph_id": "existing-model",
            "name": "Existing Model",
            "url": "https://cn.pornhub.com/model/existing-model",
            "thumbnail": "",
            "rank": 0,
            "scrape_order": 4,
            "ph_video_count": 12,
            "views_text": "1M",
        }

    @classmethod
    def prepared_model(cls):
        return {
            **cls.raw_model(),
            "name": "Existing Model Updated",
            "thumbnail": "https://ei.phncdn.com/fresh.jpg",
            "avatar_source_url": "https://ei.phncdn.com/fresh.jpg",
            "avatar_sha256": "b" * 64,
            "avatar_updated_at": datetime(2026, 7, 16, 20, 0, 0),
            "avatar_status": "ready",
            "avatar_error": "",
            "avatar_checked_at": datetime(2026, 7, 16, 20, 0, 0),
        }

    async def test_ranking_updates_only_after_avatar_is_prepared(self):
        async with self.session() as session:
            session.add(Model(
                ph_id="existing-model",
                name="Old Name",
                url="https://cn.pornhub.com/model/existing-model",
                thumbnail="https://ei.phncdn.com/legacy.jpg",
            ))
            await session.commit()

        with patch.object(
                main_module.scraper, "scrape_ranking",
                new=AsyncMock(return_value=[self.raw_model()])), patch.object(
                main_module.avatar_service, "prepare_models",
                new=AsyncMock(return_value=([self.prepared_model()], []))):
            result = await main_module.scrape_ranking(RankingRequest(
                url="https://cn.pornhub.com/user/discover/most_subscribed_models",
                max_models=1,
            ))

        async with self.session() as session:
            model = (await session.execute(select(Model))).scalar_one()
        self.assertEqual(model.thumbnail, "https://ei.phncdn.com/legacy.jpg")
        self.assertEqual(model.avatar_source_url, "https://ei.phncdn.com/fresh.jpg")
        self.assertEqual(model.avatar_sha256, "b" * 64)
        self.assertTrue(result["models"][0]["avatar_url"].endswith("b" * 16))

    async def test_ranking_with_no_verified_avatar_writes_nothing(self):
        failure = [{"ph_id": "existing-model", "error": "blocked"}]
        with patch.object(
                main_module.scraper, "scrape_ranking",
                new=AsyncMock(return_value=[self.raw_model()])), patch.object(
                main_module.avatar_service, "prepare_models",
                new=AsyncMock(return_value=([], failure))):
            with self.assertRaises(HTTPException) as raised:
                await main_module.scrape_ranking(RankingRequest(
                    url="https://cn.pornhub.com/user/discover/most_subscribed_models",
                    max_models=1,
                ))
        self.assertEqual(raised.exception.status_code, 502)
        async with self.session() as session:
            count = (await session.execute(select(func.count(Model.id)))).scalar_one()
        self.assertEqual(count, 0)

    async def test_avatar_endpoint_serves_only_the_database_hash(self):
        digest = "c" * 64
        (self.avatar_dir / f"{digest}.webp").write_bytes(b"webp-test")
        async with self.session() as session:
            model = Model(
                ph_id="served-model",
                name="Served Model",
                url="https://cn.pornhub.com/model/served-model",
                avatar_sha256=digest,
            )
            session.add(model)
            await session.commit()
            model_id = model.id

        response = await main_module.model_avatar(model_id)
        expected_path = self.avatar_dir / f"{digest}.webp"
        self.assertTrue(Path(response.path).samefile(expected_path))
        self.assertEqual(response.media_type, "image/webp")
        self.assertEqual(response.headers["cache-control"],
                         "public, max-age=31536000, immutable")


if __name__ == "__main__":
    unittest.main()
