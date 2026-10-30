from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, CloudModelFolder, DiscoveryItem, Model
from backend.main import app
import backend.main as main_module
from api_patch import patch_api


class NonLibraryPageFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = (Path(self.temp.name) / "filters.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.session() as session:
            library_model = Model(
                ph_id="in-library", profile_type="model", name="In Library",
                url="https://example/in-library", rank=1,
            )
            outside_model = Model(
                ph_id="outside", profile_type="model", name="Outside",
                url="https://example/outside", rank=2,
            )
            session.add_all([library_model, outside_model])
            await session.flush()
            session.add(CloudModelFolder(
                cid="115-cid", name="In Library", normalized_name="in library",
                model_id=library_model.id, match_method="manual", active=True,
            ))
            session.add_all([
                DiscoveryItem(
                    source="home", model_id=library_model.id, source_order=1,
                    viewkey="aaa1111111111", title="Library video",
                    url="https://example/library-video", thumbnail="https://img/library.jpg",
                ),
                DiscoveryItem(
                    source="home", model_id=outside_model.id, source_order=2,
                    viewkey="bbb2222222222", title="Outside video",
                    url="https://example/outside-video", thumbnail="https://img/outside.jpg",
                ),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_models_endpoint_never_returns_115_member(self):
        with patch_api("async_session", self.session), patch_api(
                "schedule_preview_warmup"), patch_api("schedule_preview_backfill"):
            result = await main_module.list_models()
        self.assertEqual([item["name"] for item in result["models"]], ["Outside"])
        self.assertEqual(result["library_filter"], "exclude")

    async def test_models_filter_uses_active_id_or_normalized_name(self):
        async with self.session() as session:
            same_name = Model(
                ph_id="same-normalized-name", profile_type="model",
                name="  IN   LIBRARY ", url="https://example/same-name", rank=3,
            )
            inactive_name = Model(
                ph_id="inactive-name", profile_type="model",
                name="Inactive Folder", url="https://example/inactive", rank=4,
            )
            session.add_all([same_name, inactive_name])
            await session.flush()
            session.add(CloudModelFolder(
                cid="inactive-cid", name="Inactive Folder",
                normalized_name="inactive folder", model_id=inactive_name.id,
                match_method="manual", active=False,
            ))
            await session.commit()
        main_module.library_index._invalidate_membership()
        with (
            patch_api("async_session", self.session),
            patch_api("schedule_preview_warmup"),
            patch_api("schedule_preview_backfill"),
        ):
            result = await main_module.list_models()
        self.assertEqual(
            [item["name"] for item in result["models"]],
            ["Outside", "Inactive Folder"],
        )

    async def test_models_filter_treats_legacy_null_cloud_cid_as_unbound(self):
        # Base 新表为 NOT NULL；重建一次测试表模拟历史库允许 NULL 的真实结构。
        async with self.engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE models_nullable AS SELECT * FROM models")
            await connection.exec_driver_sql("DROP TABLE models")
            await connection.exec_driver_sql(
                "ALTER TABLE models_nullable RENAME TO models")
            await connection.exec_driver_sql(
                "UPDATE models SET cloud_cid=NULL WHERE ph_id='outside'")
        main_module.library_index._invalidate_membership()
        with (
            patch_api("async_session", self.session),
            patch_api("schedule_preview_warmup"),
            patch_api("schedule_preview_backfill"),
        ):
            result = await main_module.list_models()
        self.assertEqual([item["name"] for item in result["models"]], ["Outside"])

    async def test_discovery_endpoint_never_returns_115_member(self):
        with patch_api("async_session", self.session), patch_api("schedule_preview_warmup"):
            result = await main_module.get_discovery("home")
        self.assertEqual([item["name"] for item in result["creators"]], ["Outside"])
        self.assertEqual(result["library_filter"], "exclude")


class RecommendationRemovalTests(unittest.TestCase):
    def test_recommendation_routes_are_not_registered(self):
        # 路由按领域 include_router 后，app.routes 不再平铺；以 OpenAPI 路径为准。
        paths = set(app.openapi()["paths"])
        self.assertFalse(any(path.startswith("/api/recommendations") for path in paths))
        self.assertNotIn("/api/recommendation-sources", paths)
        self.assertNotIn("/api/candidates/import", paths)


if __name__ == "__main__":
    unittest.main()
