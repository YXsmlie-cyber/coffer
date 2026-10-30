import tempfile
from pathlib import Path
import unittest

from sqlalchemy.ext.asyncio import create_async_engine

from backend.database import _enable_sqlite_wal, _migrate


class DatabaseMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_startup_enables_wal(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = (Path(directory) / "wal.db").as_posix()
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
            try:
                async with engine.begin() as connection:
                    mode = await _enable_sqlite_wal(connection)
                self.assertEqual(mode, "wal")
            finally:
                await engine.dispose()

    async def test_migration_backfills_identity_and_quarantines_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = (Path(directory) / "legacy.db").as_posix()
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
            try:
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(
                        "CREATE TABLE models (id INTEGER PRIMARY KEY, name VARCHAR, "
                        "cloud_cid VARCHAR)"
                    )
                    await connection.exec_driver_sql(
                        "CREATE TABLE videos (id INTEGER PRIMARY KEY)"
                    )
                    await connection.exec_driver_sql(
                        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, viewkey VARCHAR, "
                        "model_name VARCHAR, model_cloud_cid VARCHAR, task_type VARCHAR, "
                        "status VARCHAR, error TEXT)"
                    )
                    await connection.exec_driver_sql(
                        "INSERT INTO models(id,name,cloud_cid) VALUES "
                        "(1,'Same','cid-1'),(2,'Same','cid-2')"
                    )
                    await connection.exec_driver_sql(
                        "INSERT INTO tasks(id,viewkey,model_name,model_cloud_cid,task_type,status,error) "
                        "VALUES (1,'vk','Same','cid-1','download','completed',''),"
                        "(2,'vk','Same','cid-1','download','queued','')"
                    )
                    await _migrate(connection)

                    rows = (await connection.exec_driver_sql(
                        "SELECT id,model_id,status,error FROM tasks ORDER BY id"
                    )).fetchall()
                    indexes = (await connection.exec_driver_sql(
                        "PRAGMA index_list(tasks)"
                    )).fetchall()
                    task_columns = (await connection.exec_driver_sql(
                        "PRAGMA table_xinfo(tasks)"
                    )).fetchall()
                    model_columns = (await connection.exec_driver_sql(
                        "PRAGMA table_info(models)"
                    )).fetchall()
                    runtime_table = (await connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name='runtime_settings'"
                    )).fetchone()
                self.assertEqual(rows[0][1:3], (1, "completed"))
                self.assertIsNone(rows[1][1])
                self.assertEqual(rows[1][2], "failed")
                self.assertIn("重复任务", rows[1][3])
                self.assertIn("uq_task_model_viewkey_type", {row[1] for row in indexes})
                column_names = {row[1] for row in task_columns}
                self.assertTrue({
                    "auto_retry_count", "next_retry_at", "remote_missing_count",
                    "patrol_recovery_count", "status_rank",
                }.issubset(column_names))
                self.assertTrue({
                    "avatar_source_url", "avatar_sha256", "avatar_updated_at",
                    "avatar_status", "avatar_error", "avatar_checked_at",
                    "profile_type", "profile_slug",
                }.issubset({row[1] for row in model_columns}))
                self.assertIsNotNone(runtime_table)
            finally:
                await engine.dispose()

    async def test_generated_status_rank_migration_can_resume_or_upgrade(self):
        """table_xinfo 必须识别已有 VIRTUAL 列，版本落后时重跑不重复 ADD。"""
        with tempfile.TemporaryDirectory() as directory:
            db_path = (Path(directory) / "generated-resume.db").as_posix()
            engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
            try:
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(
                        "CREATE TABLE models (id INTEGER PRIMARY KEY, name VARCHAR, "
                        "cloud_cid VARCHAR)"
                    )
                    await connection.exec_driver_sql(
                        "CREATE TABLE videos (id INTEGER PRIMARY KEY)"
                    )
                    await connection.exec_driver_sql(
                        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, viewkey VARCHAR, "
                        "model_name VARCHAR, model_cloud_cid VARCHAR, task_type VARCHAR, "
                        "status VARCHAR, error TEXT, "
                        "status_rank INTEGER GENERATED ALWAYS AS (CASE status "
                        "WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 6 END) VIRTUAL)"
                    )
                    # 模拟上一版已经加 generated column，但版本尚未写入/仍落后。
                    await connection.exec_driver_sql(
                        "CREATE TABLE runtime_settings (key VARCHAR PRIMARY KEY, "
                        "value TEXT, updated_at DATETIME)"
                    )
                    await connection.exec_driver_sql(
                        "INSERT INTO runtime_settings(key,value) VALUES "
                        "('schema_migration_version','5')"
                    )
                    await _migrate(connection)
                    columns = (await connection.exec_driver_sql(
                        "PRAGMA table_xinfo(tasks)"
                    )).fetchall()
                    indexes = (await connection.exec_driver_sql(
                        "PRAGMA index_list(tasks)"
                    )).fetchall()
                    version = (await connection.exec_driver_sql(
                        "SELECT value FROM runtime_settings "
                        "WHERE key='schema_migration_version'"
                    )).scalar_one()
                names = [row[1] for row in columns]
                self.assertEqual(names.count("status_rank"), 1)
                self.assertEqual(version, "6")
                self.assertTrue({
                    "ix_tasks_schedule_type_status_created_id",
                    "ix_tasks_type_status_rank_id_desc",
                    "ix_tasks_status_rank_id_desc",
                }.issubset({row[1] for row in indexes}))
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
