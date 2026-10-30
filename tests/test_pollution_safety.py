import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from scripts import clean_pollution
from backend.downloader import build_remote_filename


class PollutionSafetyTests(unittest.TestCase):
    def test_old_report_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "old.json"
            report.write_text(json.dumps({"1": {"name": "Model"}}), encoding="utf-8")
            with patch.object(clean_pollution, "SCAN", report):
                with self.assertRaises(RuntimeError):
                    clean_pollution.load_scan()


class PollutionCandidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidates_require_completed_task_and_exact_generated_filename(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                "create table models(id integer primary key,name text,url text,cloud_cid text)"
            )
            connection.execute(
                "create table tasks(model_id integer,viewkey text,title text,"
                "task_type text,status text)"
            )
            connection.execute(
                "insert into models values(1,'Model','https://example/model','cid-1')"
            )
            connection.execute(
                "insert into tasks values(1,'64e9a350f176d','Tool Title','upload','completed')"
            )
            connection.execute(
                "insert into tasks values(1,'65e9a350f176e','Existing Title','upload','skipped')"
            )
            report = {
                "schema_version": 2,
                "models": {"1": {
                    "model_id": 1, "name": "Model", "url": "https://example/model",
                    "cloud_cid": "cid-1",
                    "polluted_viewkeys": ["64e9a350f176d", "65e9a350f176e"],
                }},
            }
            files = [
                {"fid": "1", "n": "User original [64e9a350f176d].mp4"},
                {"fid": "2", "n": build_remote_filename("Tool Title", "64e9a350f176d")},
                {"fid": "3", "n": build_remote_filename("Existing Title", "65e9a350f176e")},
            ]
            with patch.object(clean_pollution.cloud, "list_all_files",
                              new=AsyncMock(return_value=files)):
                candidates, skipped = await clean_pollution.build_candidates(
                    connection, report)
            self.assertEqual([item["fid"] for item in candidates], ["2"])
            self.assertEqual(skipped, [])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
