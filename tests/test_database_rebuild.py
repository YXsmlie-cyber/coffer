from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import Base, Model
from scripts.rebuild_database import rebuild_database


class DatabaseRebuildTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_refuses_in_place_or_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            source.write_bytes(b"not touched")
            with self.assertRaises(ValueError):
                await rebuild_database(source, source)

            destination = root / "existing.db"
            destination.write_bytes(b"keep me")
            with self.assertRaises(FileExistsError):
                await rebuild_database(source, destination)
            self.assertEqual(source.read_bytes(), b"not touched")
            self.assertEqual(destination.read_bytes(), b"keep me")

            colliding_source = root / "rebuilt.db.report.json"
            colliding_source.write_bytes(b"source must survive")
            with self.assertRaises(ValueError):
                await rebuild_database(colliding_source, root / "rebuilt.db")
            self.assertEqual(colliding_source.read_bytes(), b"source must survive")
            self.assertFalse((root / "rebuilt.db").exists())

            report_target = root / "with-report.db"
            existing_report = root / "with-report.db.report.json"
            existing_report.write_bytes(b"keep report")
            with self.assertRaises(FileExistsError):
                await rebuild_database(source, report_target)
            self.assertEqual(existing_report.read_bytes(), b"keep report")
            self.assertFalse(report_target.exists())

    async def test_rebuild_copies_supported_data_and_reports_reverted_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.db"
            target = root / "rebuilt.db"
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{source.as_posix()}"
            )
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                session_factory = async_sessionmaker(engine, expire_on_commit=False)
                async with session_factory() as session:
                    session.add(Model(
                        ph_id="kept",
                        name="Kept",
                        url="https://cn.pornhub.com/model/kept",
                    ))
                    await session.commit()
            finally:
                await engine.dispose()

            with closing(sqlite3.connect(source)) as connection:
                # 模拟本性能版本发布前的正常旧库：还没有两个 Model 摘要列和
                # Task generated 排序列，而不是只测试“当前库 + 多余列”。
                connection.execute("DROP INDEX IF EXISTS ix_models_library_name_key")
                connection.execute(
                    "DROP INDEX IF EXISTS ix_models_profile_library_name_id")
                connection.execute("ALTER TABLE models DROP COLUMN library_name_key")
                connection.execute("ALTER TABLE models DROP COLUMN snapshot_hash")
                connection.execute(
                    "DROP INDEX IF EXISTS ix_tasks_type_status_rank_id_desc")
                connection.execute(
                    "DROP INDEX IF EXISTS ix_tasks_status_rank_id_desc")
                connection.execute("ALTER TABLE tasks DROP COLUMN status_rank")
                connection.execute(
                    "ALTER TABLE models ADD COLUMN reverted_payload TEXT"
                )
                connection.execute(
                    "UPDATE models SET reverted_payload='legacy'"
                )
                connection.execute(
                    "CREATE TABLE reverted_feature(id INTEGER PRIMARY KEY, value TEXT)"
                )
                connection.execute(
                    "INSERT INTO reverted_feature(value) VALUES ('preserved in source')"
                )
                connection.commit()

            result = await rebuild_database(source, target)

            self.assertEqual(result["integrity_check"], "ok")
            self.assertIn("reverted_feature", result["source_only_tables"])
            self.assertIn("reverted_payload", result["source_only_columns"]["models"])
            self.assertIn("library_name_key", result["target_only_columns"]["models"])
            self.assertIn("snapshot_hash", result["target_only_columns"]["models"])
            self.assertIn("status_rank", result["target_only_columns"]["tasks"])
            self.assertTrue(source.is_file())
            self.assertTrue(target.is_file())
            self.assertTrue(Path(result["report_path"]).is_file())
            with closing(sqlite3.connect(target)) as connection:
                self.assertEqual(
                    connection.execute("SELECT ph_id FROM models").fetchone()[0],
                    "kept",
                )
                target_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(models)")
                }
                self.assertNotIn("reverted_payload", target_columns)
                rebuilt = connection.execute(
                    "SELECT library_name_key, snapshot_hash FROM models"
                ).fetchone()
                self.assertEqual(rebuilt, ("kept", ""))
                task_columns = {
                    row[1] for row in connection.execute("PRAGMA table_xinfo(tasks)")
                }
                self.assertIn("status_rank", task_columns)
                self.assertIsNone(connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='reverted_feature'"
                ).fetchone())

    async def test_rebuild_fills_old_python_defaults_and_repairs_duplicate_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "minimal-legacy.db"
            target = root / "minimal-rebuilt.db"
            with closing(sqlite3.connect(source)) as connection:
                connection.executescript("""
                    CREATE TABLE models (
                        id INTEGER PRIMARY KEY,
                        ph_id VARCHAR NOT NULL,
                        name VARCHAR NOT NULL,
                        url VARCHAR NOT NULL
                    );
                    INSERT INTO models(id, ph_id, name, url) VALUES (
                        1, 'legacy', '  Straße\t Name  ',
                        'https://cn.pornhub.com/model/legacy'
                    );
                    CREATE TABLE tasks (
                        id INTEGER PRIMARY KEY,
                        viewkey VARCHAR NOT NULL,
                        model_id INTEGER,
                        model_name VARCHAR NOT NULL,
                        title VARCHAR NOT NULL,
                        url VARCHAR NOT NULL,
                        task_type VARCHAR NOT NULL,
                        status VARCHAR NOT NULL
                    );
                    INSERT INTO tasks VALUES (
                        1, 'same-key', 1, 'Legacy', 'First', 'https://example/1',
                        'download', 'queued'
                    );
                    INSERT INTO tasks VALUES (
                        2, 'same-key', 1, 'Legacy', 'Second', 'https://example/2',
                        'download', 'queued'
                    );
                """)
                connection.commit()

            result = await rebuild_database(source, target)

            self.assertEqual(result["validated_table_counts"]["models"], 1)
            self.assertEqual(result["validated_table_counts"]["tasks"], 2)
            self.assertIn("profile_type", result["defaulted_target_columns"]["models"])
            self.assertIn("created_at", result["defaulted_target_columns"]["models"])
            with closing(sqlite3.connect(target)) as connection:
                model = connection.execute(
                    "SELECT library_name_key, profile_type, profile_slug, "
                    "avatar_status, created_at FROM models"
                ).fetchone()
                self.assertEqual(model[:4], (
                    "strasse name", "model", "legacy", "pending",
                ))
                self.assertTrue(model[4])
                tasks = connection.execute(
                    "SELECT model_id, status FROM tasks ORDER BY id"
                ).fetchall()
                self.assertEqual(sum(row[0] == 1 for row in tasks), 1)
                self.assertEqual(sum(row[0] is None for row in tasks), 1)
                self.assertIn("failed", {row[1] for row in tasks})
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='uq_task_model_viewkey_type'"
                ).fetchone())

    async def test_rebuild_rejects_foreign_key_corruption_without_partial_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "broken-fk.db"
            target = root / "must-not-exist.db"
            with closing(sqlite3.connect(source)) as connection:
                connection.executescript("""
                    CREATE TABLE models (
                        id INTEGER PRIMARY KEY,
                        ph_id VARCHAR NOT NULL,
                        name VARCHAR NOT NULL,
                        url VARCHAR NOT NULL
                    );
                    CREATE TABLE videos (
                        id INTEGER PRIMARY KEY,
                        model_id INTEGER NOT NULL,
                        viewkey VARCHAR NOT NULL,
                        title VARCHAR NOT NULL,
                        url VARCHAR NOT NULL
                    );
                    INSERT INTO videos VALUES (
                        1, 999, 'orphan', 'Orphan', 'https://example/orphan'
                    );
                """)
                connection.commit()
            source_bytes = source.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "外键错误"):
                await rebuild_database(source, target)

            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(
                target.suffix + ".report.json").exists())

    async def test_publish_race_preserves_competing_report_and_removes_own_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (root / "source.db").resolve()
            target = (root / "target.db").resolve()
            report = target.with_suffix(target.suffix + ".report.json")
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("""
                    CREATE TABLE models(
                        id INTEGER PRIMARY KEY,
                        ph_id VARCHAR NOT NULL,
                        name VARCHAR NOT NULL,
                        url VARCHAR NOT NULL
                    )
                """)
                connection.execute(
                    "INSERT INTO models VALUES (1, 'one', 'One', 'https://example/one')"
                )
                connection.commit()
            source_bytes = source.read_bytes()
            # Python 3.14.7 的 pathlib 具体路径类可能覆盖 Path.open；补丁必须
            # 落在当前平台实际使用的 WindowsPath/PosixPath 上，才能稳定模拟
            # exists 检查后、原子占位前由竞争者创建报告文件的时序。
            path_type = type(report)
            original_open = path_type.open

            race_injected = False

            def racing_open(path, mode="r", *args, **kwargs):
                nonlocal race_injected
                if (path.resolve(strict=False) == report.resolve(strict=False)
                        and mode == "xb" and not report.exists()):
                    with original_open(report, "wb") as stream:
                        stream.write(b"created by another process")
                    race_injected = True
                return original_open(path, mode, *args, **kwargs)

            with patch.object(path_type, "open", new=racing_open):
                with self.assertRaises(FileExistsError):
                    await rebuild_database(source, target)

            self.assertTrue(race_injected)
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(report.read_bytes(), b"created by another process")
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
