"""审美推荐的产品化行为：推荐说明、命中率历史、批量评价、隐藏已入库与封面补齐时限。"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import preview_image_store, taste_visual
from backend.database import (
    Base,
    CloudModelFolder,
    FeatureVector,
    Model,
    ModelPreviewVideo,
    TasteModelRun,
    TasteModelScore,
    TasteResultFeedback,
)
from backend.scraper import resolve_video_poster
from backend import taste_model
from backend.taste_model import (
    LIBRARY_RUN_PREFIX,
    TasteModelService,
    TasteModelUnavailable,
    build_label_dataset,
    validate_library_ranking,
)
from backend.taste_visual import (
    FEATURE_AVATAR, FEATURE_COVER, FeatureIndexResult, taste_visual_features,
)

VISUAL_STATUS = {"available": True, "error": "", "model_version": "test-version"}


class TasteProductTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = (Path(self.temp.name) / "taste-product.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        from backend.library_index import library_index
        library_index._invalidate_membership()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def add_models(self, session, names):
        models = [Model(ph_id=f"p-{name}", profile_type="model", profile_slug=f"p-{name}",
                        name=name, url=f"https://example/{name}", avatar_status="ready",
                        avatar_sha256=f"{index:064d}")
                  for index, name in enumerate(names, start=1)]
        session.add_all(models)
        await session.flush()
        return {model.name: model for model in models}

    @staticmethod
    def vectors(model_id, value):
        vector = np.asarray(value, dtype="<f4")
        return [FeatureVector(model_id=model_id, feature_type=kind, item_key=key,
                              model_version="test-version", input_hash=kind[0] * 64,
                              dimensions=2, vector=vector.tobytes(), status="ready")
                for kind, key in ((FEATURE_AVATAR, "avatar"), (FEATURE_COVER, "first_cover"))]

    async def published_run(self, session, ranked_models):
        run = TasteModelRun(comparison_hash=build_label_dataset([]).comparison_hash,
                            model_version="test-version", status="ready",
                            quality_passed=True, published=True)
        session.add(run)
        await session.flush()
        session.add_all([TasteModelScore(run_id=run.id, model_id=model.id, rank=rank, score=1.0 / rank)
                         for rank, model in ranked_models])
        return run

    async def state(self, service):
        with patch.object(taste_visual_features, "status", return_value=VISUAL_STATUS), \
                patch("backend.taste_visual.load_visual_manifest", return_value={"version": "test-version"}):
            return await service.get_state(session_factory=self.session)

    async def test_results_explain_nearest_liked_and_hide_new_library_models(self):
        async with self.session() as session:
            m = await self.add_models(session, ["Liked East", "Liked North", "Cand East",
                                                "Cand North", "Now In 115", "No Vector"])
            for name, value in (("Liked East", [1, 0]), ("Liked North", [0, 1]),
                                ("Cand East", [0.9, 0.1]), ("Cand North", [0.2, 0.8]),
                                ("Now In 115", [1, 0])):
                session.add_all(self.vectors(m[name].id, value))
            old = TasteModelRun(comparison_hash="old", model_version="test-version", status="ready")
            session.add(old)
            await session.flush()
            session.add_all([TasteResultFeedback(model_id=m[name].id, run_id=old.id,
                                                 action="like", active=True)
                             for name in ("Liked East", "Liked North")])
            await self.published_run(session, [(1, m["Cand East"]), (2, m["Cand North"]),
                                               (3, m["Now In 115"]), (4, m["No Vector"])])
            session.add(CloudModelFolder(cid="lib-1", name="Now In 115", normalized_name="now in 115",
                                         model_id=m["Now In 115"].id, match_method="manual", active=True))
            await session.commit()

        state = await self.state(TasteModelService())
        names = [item["model"]["name"] for item in state["results"]]
        self.assertEqual(names, ["Cand East", "Cand North", "No Vector"])
        self.assertEqual(state["results_hidden_in_library"], 1)
        by_name = {item["model"]["name"]: item for item in state["results"]}
        self.assertEqual(by_name["Cand East"]["similar_to"]["name"], "Liked East")
        self.assertIn("Liked East", by_name["Cand East"]["reason"])
        self.assertEqual(by_name["Cand North"]["similar_to"]["name"], "Liked North")
        self.assertIsNone(by_name["No Vector"]["similar_to"])
        self.assertIn("整体风格", by_name["No Vector"]["reason"])

    async def test_history_reports_like_rate_per_batch(self):
        async with self.session() as session:
            m = await self.add_models(session, [f"H{i}" for i in range(6)])
            first = TasteModelRun(comparison_hash="a", model_version="v", status="ready")
            second = TasteModelRun(comparison_hash="b", model_version="v", status="ready")
            session.add_all([first, second])
            await session.flush()
            session.add_all([
                TasteResultFeedback(model_id=m["H0"].id, run_id=first.id, action="like", active=True),
                TasteResultFeedback(model_id=m["H1"].id, run_id=first.id, action="like", active=True),
                TasteResultFeedback(model_id=m["H2"].id, run_id=first.id, action="dislike", active=True),
                TasteResultFeedback(model_id=m["H3"].id, run_id=first.id, action="like", active=False),
                TasteResultFeedback(model_id=m["H4"].id, run_id=second.id, action="dislike", active=True),
                TasteResultFeedback(model_id=m["H5"].id, run_id=second.id, action="dislike", active=True),
            ])
            await session.commit()
            first_id, second_id = first.id, second.id
        history = await TasteModelService()._history(session_factory=self.session)
        self.assertEqual([(row["run_id"], row["liked"], row["disliked"]) for row in history],
                         [(first_id, 2, 1), (second_id, 0, 2)])
        self.assertEqual(history[0]["like_rate"], round(2 / 3, 4))
        self.assertEqual([row["batch"] for row in history], [1, 2])
        self.assertEqual(history[1]["like_rate"], 0.0)

    async def test_batch_feedback_records_all_or_nothing(self):
        async with self.session() as session:
            m = await self.add_models(session, ["B1", "B2", "B3", "Outside"])
            run = await self.published_run(session, [(1, m["B1"]), (2, m["B2"]), (3, m["B3"]),
                                                     (120, m["Outside"])])
            session.add(TasteResultFeedback(model_id=m["B1"].id, run_id=run.id, action="like", active=True))
            await session.commit()
            ids = {name: model.id for name, model in m.items()}
        service = TasteModelService()
        with self.assertRaises(TasteModelUnavailable):
            await service.record_feedback_many([ids["B2"], ids["Outside"]], "dislike",
                                               session_factory=self.session)
        result = await service.record_feedback_many([ids["B1"], ids["B2"]], "dislike",
                                                    session_factory=self.session)
        self.assertEqual(result["model_ids"], sorted([ids["B1"], ids["B2"]]))
        async with self.session() as session:
            active = {(row.model_id, row.action) for row in (await session.execute(
                select(TasteResultFeedback).where(TasteResultFeedback.active.is_(True))
            )).scalars().all()}
        self.assertEqual(active, {(ids["B1"], "dislike"), (ids["B2"], "dislike")})
        with self.assertRaises(ValueError):
            await service.record_feedback_many([ids["B3"]], "maybe", session_factory=self.session)

    async def test_cover_fetch_defers_rest_only_when_coverage_is_safe(self):
        async with self.session() as session:
            m = await self.add_models(session, ["Cached", "Slow One", "Slow Two"])
            for index, name in enumerate(("Cached", "Slow One", "Slow Two")):
                session.add(ModelPreviewVideo(
                    model_id=m[name].id, ordinal=0, viewkey=f"6ab{index:010d}", title=name,
                    url=f"https://example/v/{index}", thumbnail=f"https://example/t/{index}.jpg"))
            await session.commit()
            ids = [model.id for model in m.values()]
        cached = Path(self.temp.name) / "cached.webp"
        cached.write_bytes(b"x" * 256)

        async def never_finishes(_viewkey):
            await asyncio.sleep(30)

        def preview_path(viewkey, require_exists=True):
            return cached if viewkey == "6ab0000000000" else None

        remembered = Mock()
        with patch.object(taste_visual, "COVER_TIMEOUT_CAP_SECONDS", 0.05), \
                patch.object(taste_visual, "COVER_FETCH_BUDGET_SECONDS", 0.0), \
                patch.object(preview_image_store, "preview_path", side_effect=preview_path), \
                patch.object(preview_image_store, "ensure_preview_image", side_effect=never_finishes), \
                patch.object(preview_image_store, "remember_failure", remembered):
            # 已有 1/3 封面 ≥ 30%：时限一到剩余的直接留到下一批，不再白等。
            *_rest, total, succeeded, deferred = await taste_visual_features._prepare_assets(
                ids, fetch_missing_covers=True, progress=None,
                session_factory=self.session, min_cover_ratio=0.3)
            self.assertEqual((total, succeeded, deferred), (2, 0, 2))
            remembered.assert_not_called()
            # 不设覆盖率门槛（如首次全量补齐）时不提前收手；超时的封面登记冷却。
            *_rest, total, succeeded, deferred = await taste_visual_features._prepare_assets(
                ids, fetch_missing_covers=True, progress=None, session_factory=self.session)
            self.assertEqual((total, succeeded, deferred), (2, 0, 0))
            self.assertEqual(sorted(call.args[0] for call in remembered.call_args_list),
                             ["6ab0000000001", "6ab0000000002"])


def _unit(*values):
    vector = np.asarray(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


class LibraryValidationTests(unittest.TestCase):
    def test_too_few_ratings_publishes_as_unvalidated(self):
        positives = {1: _unit(1, 0), 2: _unit(1, 0.1)}
        result = validate_library_ranking(
            positives, [_unit(0, 1), _unit(0.1, 1)], {10: 1, 11: 0},
            {10: _unit(1, 0), 11: _unit(0, 1)}, apply_gate=True)
        self.assertFalse(result["validated"])
        self.assertIsNone(result["passed"], "评价不足不能判失败，也不能假装通过")

    def test_rated_models_are_held_out_and_gate_applies(self):
        positives = {model_id: _unit(1, 0.05 * model_id) for model_id in range(1, 6)}
        background = [_unit(0.05 * index, 1) for index in range(6)]
        likes = {100 + index: 1 for index in range(8)}
        dislikes = {200 + index: 0 for index in range(8)}
        labels = {**likes, **dislikes}
        features = {model_id: (_unit(1, 0.1) if label else _unit(0.1, 1))
                    for model_id, label in labels.items()}
        # 评价过的“符合”即使也在收藏里，检验时也不能参与计算方向。
        leaked = {**positives, **{model_id: features[model_id] for model_id in likes}}
        result = validate_library_ranking(
            leaked, background, labels, features, apply_gate=True)
        self.assertTrue(result["validated"])
        self.assertEqual(result["labeled_models"], 16)
        self.assertGreaterEqual(result["auc"], 0.99)
        self.assertTrue(result["passed"])

        flipped = {model_id: 1 - label for model_id, label in labels.items()}
        failed = validate_library_ranking(
            positives, background, flipped, features, apply_gate=True)
        self.assertFalse(failed["passed"])


class LibraryModeTests(TasteProductTests):
    """收藏模式：115 收藏为正例，打开页面即可自动生成，不需要比较。"""

    async def _library_fixture(self):
        async with self.session() as session:
            names = ["Lib A", "Lib B", "Lib C", "Near", "Mid", "Far"]
            m = await self.add_models(session, names)
            values = {"Lib A": [1, 0], "Lib B": [0.95, 0.1], "Lib C": [0.9, 0.2],
                      "Near": [0.9, 0.15], "Mid": [0.5, 0.5], "Far": [0, 1]}
            for name, value in values.items():
                session.add_all(self.vectors(m[name].id, value))
            for index, name in enumerate(["Near", "Mid", "Far"]):
                session.add(ModelPreviewVideo(
                    model_id=m[name].id, ordinal=0, viewkey=f"6ac{index:010d}", title=name,
                    url=f"https://example/v/{index}", thumbnail=f"https://example/t/{index}.jpg"))
            for name in ("Lib A", "Lib B", "Lib C"):
                m[name].cloud_cid = f"cid-{name}"
            await session.commit()
        from backend.library_index import library_index
        library_index._invalidate_membership()
        return m

    def _patches(self):
        async def fake_index(model_ids, **_kwargs):
            count = len(set(model_ids))
            return FeatureIndexResult(count, count, 0, 0, 2 * count, 0, 0, "test-version", ())
        return [
            patch.object(taste_visual_features, "status", return_value=VISUAL_STATUS),
            patch("backend.taste_visual.load_visual_manifest",
                  return_value={"version": "test-version"}),
            patch.object(taste_visual_features, "index_models", side_effect=fake_index),
            patch("backend.taste_model.avatar_store.avatar_path",
                  return_value=Path("avatar.webp")),
            patch.object(taste_model, "MIN_LIBRARY_POSITIVES", 2),
        ]

    async def test_library_run_ranks_by_collection_without_comparisons(self):
        m = await self._library_fixture()
        service = TasteModelService()
        patches = self._patches()
        for item in patches:
            item.start()
        try:
            before = await service.get_state(session_factory=self.session)
            self.assertTrue(before["auto_start"], "还没有收藏模式结果时应自动生成")
            self.assertEqual(before["library"]["members"], 3)
            state = await service.run_now(session_factory=self.session, mode="library")
        finally:
            for item in reversed(patches):
                item.stop()
        self.assertEqual(state["status"], "ready")
        names = [item["model"]["name"] for item in state["results"]]
        self.assertEqual(names, ["Near", "Mid", "Far"])
        self.assertEqual(state["results"][0]["similar_label"], "收藏")
        self.assertIn(state["results"][0]["similar_to"]["name"], {"Lib A", "Lib B", "Lib C"})
        self.assertFalse(state["run"]["metrics"]["formal"]["validated"])
        self.assertFalse(state["auto_start"], "刚生成的结果不需要再自动生成")
        async with self.session() as session:
            run = await session.get(TasteModelRun, state["published_run_id"])
        self.assertTrue(run.comparison_hash.startswith(LIBRARY_RUN_PREFIX))

        # 收藏有变化 -> 结果过期 -> 打开页面会自动刷新；进了 115 的结果被隐藏。
        async with self.session() as session:
            far = await session.get(Model, m["Far"].id)
            far.cloud_cid = "cid-new"
            await session.commit()
        from backend.library_index import library_index
        library_index._invalidate_membership()
        changed = await self.state(service)
        self.assertTrue(changed["results_stale"])
        self.assertTrue(changed["auto_start"])
        self.assertEqual(changed["results_hidden_in_library"], 1)

    async def test_recent_failure_pauses_automatic_retries(self):
        await self._library_fixture()
        async with self.session() as session:
            session.add(TasteModelRun(
                comparison_hash=LIBRARY_RUN_PREFIX + "x", model_version="test-version",
                status="failed", error="网络中断", completed_at=datetime.now()))
            await session.commit()
        state = await self.state(TasteModelService())
        self.assertFalse(state["auto_start"])
        self.assertTrue(state["can_start"], "手动重新生成仍然可以")
        async with self.session() as session:
            run = (await session.execute(select(TasteModelRun))).scalars().one()
            run.completed_at = datetime.now() - timedelta(hours=1)
            await session.commit()
        self.assertTrue((await self.state(TasteModelService()))["auto_start"])

class PosterAndFailureCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_removed_video_page_resolves_to_unavailable_without_backoff(self):
        viewkey = "6ab0000000009"
        removed = ("<html><title>取消播放视频</title><div class='removed'>此视频已下架</div>"
                   + "x" * 80_000 + "</html>")
        # 地区限制/私密页：同一个“此页面不可用”外壳，曾经让封面请求挂约 3 分钟才 404。
        geo_blocked = ("<html><body><div class='geoBlocked'><h1>此页面不可用</h1></div>"
                       + "x" * 80_000 + "</body></html>")
        for html in (removed, geo_blocked):
            response = SimpleNamespace(
                status_code=200, text=html,
                url=f"https://cn.pornhub.com/view_video.php?viewkey={viewkey}")

            class Client:
                cookies = SimpleNamespace(set=lambda *a, **k: None)

                async def get(self, _url, _response=response):
                    return _response

            sleeper = AsyncMock()
            with patch("backend.scraper.asyncio.sleep", new=sleeper), \
                    patch("backend.scraper._solve_challenge", return_value=None):
                self.assertIsNone(await resolve_video_poster(Client(), viewkey))
            sleeper.assert_not_awaited()

    async def test_remembered_failure_short_circuits_next_fetch(self):
        viewkey = "6ab0000000010"
        preview_image_store.remember_failure(viewkey, "封面抓取超时", 60)
        try:
            with patch.object(preview_image_store, "preview_path", return_value=None), \
                    patch.object(preview_image_store, "_resolve_poster_url",
                                 side_effect=AssertionError("不应再访问网络")):
                with self.assertRaises(preview_image_store.PreviewImageTransientError):
                    await preview_image_store.ensure_preview_image(viewkey)
        finally:
            preview_image_store._failure_cache.pop(viewkey, None)
