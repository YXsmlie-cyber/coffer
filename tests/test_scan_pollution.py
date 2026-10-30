from pathlib import Path
from contextlib import closing
import io
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from scripts import scan_pollution


class PollutionCheckpointTests(unittest.TestCase):
    def test_console_output_survives_narrow_windows_encoding(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252")
        with patch.object(scan_pollution.sys, "stdout", stream):
            scan_pollution._console_print("中文🙂", flush=True)

        rendered = raw.getvalue().decode("cp1252")
        self.assertIn(r"\u4e2d\u6587", rendered)
        self.assertIn(r"\U0001f642", rendered)

    def test_append_journal_recovers_without_rewriting_full_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "pollution.json"
            journal = root / "pollution.json.journal.jsonl"
            items = [
                {"model_id": 1, "polluted_count": 2},
                {"model_id": 2, "polluted_count": 0},
            ]
            with patch.object(scan_pollution, "OUT", output), \
                    patch.object(scan_pollution, "JOURNAL", journal):
                scan_pollution.append_checkpoint(items)
                self.assertFalse(output.exists())
                recovered = scan_pollution.load_progress()

            self.assertEqual(recovered["models"]["1"], items[0])
            self.assertEqual(recovered["models"]["2"], items[1])
            self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 2)

    def test_replay_ignores_only_invalid_journal_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "pollution.json"
            journal = root / "pollution.json.journal.jsonl"
            with patch.object(scan_pollution, "OUT", output), \
                    patch.object(scan_pollution, "JOURNAL", journal):
                scan_pollution.save_progress({
                    "schema_version": scan_pollution.SCHEMA_VERSION,
                    "models": {"1": {"model_id": 1, "polluted_count": 1}},
                })
                scan_pollution.append_checkpoint([
                    {"model_id": 1, "polluted_count": 3},
                ])
                with journal.open("a", encoding="utf-8") as stream:
                    stream.write('{"truncated"\n')
                recovered = scan_pollution.load_progress()

            self.assertEqual(recovered["models"]["1"]["polluted_count"], 3)


class PollutionSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "missing.db"
            with patch.object(scan_pollution, "DB", database), \
                    patch.object(scan_pollution, "OUT", root / "out.json"), \
                    patch.object(scan_pollution, "JOURNAL", root / "journal.jsonl"):
                with self.assertRaises(sqlite3.OperationalError):
                    await scan_pollution.main()
            self.assertFalse(database.exists())

    async def test_database_snapshot_is_closed_before_network_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "scan.db"
            output = root / "out.json"
            journal = root / "journal.jsonl"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript("""
                    CREATE TABLE models(
                        id INTEGER PRIMARY KEY, name TEXT, url TEXT, cloud_cid TEXT
                    );
                    CREATE TABLE tasks(
                        model_id INTEGER, viewkey TEXT, task_type TEXT, status TEXT
                    );
                    INSERT INTO models VALUES (
                        1, 'Model', 'https://example/model', 'cid-1'
                    );
                    INSERT INTO tasks VALUES (1, 'clean-key', 'upload', 'completed');
                """)
                connection.commit()

            exclusive_acquired = False

            async def scrape(_url, **_kwargs):
                nonlocal exclusive_acquired
                # 若启动快照仍持有读事务，DELETE journal 下的 EXCLUSIVE 会失败；
                # 这里也直接覆盖 Windows 上文件句柄及时释放的要求。
                with closing(sqlite3.connect(database, timeout=0.05)) as connection:
                    connection.execute("BEGIN EXCLUSIVE")
                    exclusive_acquired = True
                    connection.rollback()
                return [{"viewkey": "clean-key"}]

            with patch.object(scan_pollution, "DB", database), \
                    patch.object(scan_pollution, "OUT", output), \
                    patch.object(scan_pollution, "JOURNAL", journal), \
                    patch.object(scan_pollution, "scrape_model_videos",
                                 new=AsyncMock(side_effect=scrape)):
                await scan_pollution.main()

            self.assertTrue(exclusive_acquired)
            self.assertTrue(output.is_file())
            self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
