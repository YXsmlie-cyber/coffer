from datetime import datetime
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, Model
import backend.avatar_repair as repair_module


class AvatarRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_repair_skips_ready_assets_and_commits_prepared_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            avatar_dir = root / "avatars"
            avatar_dir.mkdir()
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{(root / 'repair.db').as_posix()}")
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

            ready_hash = "1" * 64
            repaired_hash = "2" * 64
            (avatar_dir / f"{ready_hash}.webp").write_bytes(b"ready")
            (avatar_dir / f"{repaired_hash}.webp").write_bytes(b"repaired")
            previous_log = root / "avatar-repair-failures.jsonl"
            previous_log.write_text('{"error":"previous interrupted run"}\n',
                                    encoding="utf-8")
            async with session_factory() as session:
                session.add_all([
                    Model(
                        ph_id="ready", name="Ready",
                        url="https://cn.pornhub.com/model/ready",
                        avatar_sha256=ready_hash,
                    ),
                    Model(
                        ph_id="missing", name="Missing",
                        url="https://cn.pornhub.com/model/missing",
                    ),
                ])
                await session.commit()

            async def prepare(items, **_kwargs):
                self.assertEqual([item["ph_id"] for item in items], ["missing"])
                item = {
                    **items[0],
                    "name": "Missing  Straße\tName",
                    "avatar_source_url": "https://ei.phncdn.com/fresh.jpg",
                    "avatar_sha256": repaired_hash,
                    "avatar_updated_at": datetime(2026, 7, 16, 20, 0, 0),
                    "avatar_status": "ready",
                    "avatar_error": "",
                    "avatar_checked_at": datetime(2026, 7, 16, 20, 0, 0),
                }
                return [item], []

            with patch.object(repair_module, "async_session", session_factory), \
                    patch.object(repair_module, "DATA_DIR", root), \
                    patch.object(repair_module.avatar_store.settings, "avatar_dir", str(avatar_dir)), \
                    patch.object(repair_module, "init_db", new=AsyncMock()), \
                    patch.object(repair_module.avatar_service, "prepare_models", side_effect=prepare):
                report = await repair_module.repair_avatars(batch_size=10)

            async with session_factory() as session:
                models = (await session.execute(select(Model).order_by(Model.id))).scalars().all()
            self.assertEqual(report["selected"], 1)
            self.assertEqual(report["completed"], 1)
            self.assertEqual(report["remaining_without_local_avatar"], 0)
            archived_log = Path(report["previous_failure_log"])
            self.assertTrue(archived_log.is_file())
            self.assertIn("previous interrupted run",
                          archived_log.read_text(encoding="utf-8"))
            self.assertEqual(models[0].avatar_sha256, ready_hash)
            self.assertEqual(models[1].avatar_sha256, repaired_hash)
            self.assertEqual(models[1].name, "Missing  Straße\tName")
            self.assertEqual(models[1].library_name_key, "missing strasse name")
            await engine.dispose()

    async def test_commit_rejects_prepared_result_after_concurrent_profile_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = (Path(directory) / "concurrent.db").as_posix()
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with session_factory() as session:
                model = Model(
                    ph_id="stable-id",
                    name="Old Name",
                    url="https://cn.pornhub.com/model/old-name",
                    avatar_status="pending",
                )
                session.add(model)
                await session.commit()
                prepared = {
                    **repair_module._model_input(model),
                    "name": "Prepared Old Name",
                    "url": "https://cn.pornhub.com/model/prepared-old-name",
                    "avatar_source_url": "https://ei.phncdn.com/old.jpg",
                    "avatar_sha256": "8" * 64,
                    "avatar_updated_at": datetime(2026, 7, 16, 20, 0, 0),
                    "avatar_checked_at": datetime(2026, 7, 16, 20, 0, 0),
                }

            async with session_factory() as session:
                current = await session.get(Model, model.id)
                current.name = "Canonical Name"
                current.url = "https://cn.pornhub.com/model/canonical-name"
                current.avatar_sha256 = "9" * 64
                current.avatar_status = "ready"
                await session.commit()

            with patch.object(repair_module, "async_session", session_factory):
                committed, failures, _retries = await repair_module._commit_prepared(
                    [prepared]
                )

            self.assertEqual(committed, 0)
            self.assertEqual(len(failures), 1)
            async with session_factory() as session:
                current = await session.get(Model, model.id)
            self.assertEqual(current.name, "Canonical Name")
            self.assertEqual(current.library_name_key, "canonical name")
            self.assertEqual(current.avatar_sha256, "9" * 64)
            await engine.dispose()

    async def test_database_lock_is_retried_without_repeating_network_work(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "locked.db"
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{db_path.as_posix()}",
                connect_args={"timeout": 0.01},
            )
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with session_factory() as session:
                model = Model(
                    ph_id="locked", name="Locked",
                    url="https://cn.pornhub.com/model/locked",
                )
                session.add(model)
                await session.commit()
                model_id = model.id

            prepared = [{
                "_model_id": model_id,
                "ph_id": "locked",
                "name": "Locked Updated",
                "url": "https://cn.pornhub.com/model/locked",
                "avatar_source_url": "https://ei.phncdn.com/locked.jpg",
                "avatar_sha256": "3" * 64,
                "avatar_updated_at": datetime(2026, 7, 16, 20, 0, 0),
                "avatar_status": "ready",
                "avatar_error": "",
                "avatar_checked_at": datetime(2026, 7, 16, 20, 0, 0),
            }]
            lock = sqlite3.connect(db_path, timeout=0.01)
            lock.execute("BEGIN IMMEDIATE")

            async def release_lock():
                await asyncio.sleep(0.12)
                lock.rollback()

            releaser = asyncio.create_task(release_lock())
            try:
                with patch.object(repair_module, "async_session", session_factory):
                    committed, failures, retries = await repair_module._commit_prepared(prepared)
                await releaser
            finally:
                if not releaser.done():
                    await releaser
                lock.close()

            self.assertEqual(committed, 1)
            self.assertEqual(failures, [])
            self.assertGreaterEqual(retries, 1)
            async with session_factory() as session:
                updated = (await session.execute(select(Model))).scalar_one()
            self.assertEqual(updated.avatar_sha256, "3" * 64)
            await engine.dispose()

    async def test_canonical_repair_keeps_database_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{(root / 'canonical.db').as_posix()}")
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with session_factory() as session:
                model = Model(
                    ph_id="old-spelling", name="Old Spelling",
                    url="https://cn.pornhub.com/model/old-spelling",
                )
                session.add(model)
                await session.commit()
                model_id = model.id

            profile_client = AsyncMock()
            profile_context = AsyncMock()
            profile_context.__aenter__.return_value = profile_client
            image_client = AsyncMock()
            image_context = AsyncMock()
            image_context.__aenter__.return_value = image_client
            asset = MagicMock(
                source_url="https://ei.phncdn.com/canonical.jpg",
                sha256="4" * 64,
            )
            with patch.object(repair_module, "async_session", session_factory), \
                    patch.object(repair_module, "init_db", new=AsyncMock()), \
                    patch.object(repair_module.scraper, "_client", return_value=profile_context), \
                    patch.object(repair_module.scraper, "fetch_model_profile_url", new=AsyncMock(
                        return_value={
                            "ph_id": "canonical-spelling",
                            "name": "Canonical Spelling",
                            "url": "https://cn.pornhub.com/model/canonical-spelling",
                            "thumbnail": "https://ei.phncdn.com/canonical.jpg",
                        })), \
                    patch.object(repair_module.avatar_store, "create_client",
                                 return_value=image_context), \
                    patch.object(repair_module.avatar_store, "cache_avatar",
                                 new=AsyncMock(return_value=asset)):
                result = await repair_module.repair_canonical_avatar(
                    model_id, "https://cn.pornhub.com/model/canonical-spelling")

            async with session_factory() as session:
                updated = await session.get(Model, model_id)
            self.assertEqual(updated.ph_id, "old-spelling")
            self.assertEqual(updated.name, "Canonical Spelling")
            self.assertEqual(updated.library_name_key, "canonical spelling")
            self.assertEqual(updated.url, "https://cn.pornhub.com/model/canonical-spelling")
            self.assertEqual(updated.avatar_sha256, "4" * 64)
            self.assertEqual(result["canonical_ph_id"], "canonical-spelling")
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
