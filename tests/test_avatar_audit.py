from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import backend.avatar_audit as audit_module
from backend.avatar_audit import _classify_evidence
from backend.database import Base, Model


class AvatarAuditTests(unittest.TestCase):
    def test_classifies_removed_profile(self):
        status, error = _classify_evidence({
            "final_url": "https://cn.pornhub.com/pornstars",
            "h1": "最热门的色情明星",
        })
        self.assertEqual(status, "source_unavailable")
        self.assertIn("不可用", error)

    def test_classifies_profile_without_avatar(self):
        status, _ = _classify_evidence({
            "final_url": "https://cn.pornhub.com/pornstar/example",
            "has_profile_header": True,
            "avatar_attribute": "",
        })
        self.assertEqual(status, "source_no_avatar")

    def test_classifies_broken_avatar_source(self):
        status, _ = _classify_evidence({
            "final_url": "https://cn.pornhub.com/pornstar/example",
            "has_profile_header": True,
            "avatar_attribute": "https://ei.phncdn.com/dead.jpg",
        })
        self.assertEqual(status, "source_broken")

    def test_unavailable_page_does_not_depend_on_localized_h1_text(self):
        status, _ = _classify_evidence({
            "status_code": 200,
            "final_url": "https://cn.pornhub.com/pornstar/removed",
            "h1": "��ҳ�治����",
            "has_profile_header": False,
            "avatar_attribute": "",
        })
        self.assertEqual(status, "source_unavailable")


class AvatarAuditApplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_apply_batches_identity_reads_and_preserves_ready_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = (Path(directory) / "audit.db").as_posix()
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with session_factory() as session:
                session.add_all([
                    Model(
                        ph_id="missing", name="Missing",
                        url="https://cn.pornhub.com/model/missing",
                    ),
                    Model(
                        ph_id="ready", name="Ready",
                        url="https://cn.pornhub.com/model/ready",
                        avatar_sha256="a" * 64,
                        avatar_status="ready",
                    ),
                    Model(
                        ph_id="stale", name="Stale",
                        url="https://cn.pornhub.com/model/stale",
                        avatar_sha256="b" * 64,
                        avatar_status="ready",
                    ),
                ])
                await session.commit()
                rows = list((await session.execute(
                    audit_module.select(Model).order_by(Model.id)
                )).scalars())

            report = {"results": [{
                "id": row.id,
                "ph_id": row.ph_id,
                "status_code": 200,
                "final_url": row.url,
                "has_profile_header": False,
                "avatar_attribute": "",
            } for row in rows]}
            try:
                with patch.object(audit_module, "async_session", session_factory), \
                        patch.object(
                            audit_module.avatar_store,
                            "avatar_path",
                            side_effect=lambda value: (
                                Path("ready.webp") if value == "a" * 64 else None
                            ),
                        ):
                    result = await audit_module.apply_audit_statuses(report)

                self.assertEqual(result["applied"], 2)
                self.assertEqual(result["skipped_ready"], 1)
                async with session_factory() as session:
                    updated = list((await session.execute(
                        audit_module.select(Model).order_by(Model.id)
                    )).scalars())
                self.assertEqual(updated[0].avatar_status, "source_unavailable")
                self.assertEqual(updated[1].avatar_status, "ready")
                self.assertEqual(updated[2].avatar_status, "source_unavailable")
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
