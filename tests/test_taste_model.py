from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta
import json
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.database import (
    Base,
    Candidate,
    CloudModelFolder,
    FeatureVector,
    Model,
    ModelPreviewVideo,
    Task,
    TasteModelRun,
    TasteModelScore,
    TasteResultFeedback,
)
from backend.taste_model import (
    TasteModelService,
    TasteModelUnavailable,
    build_label_dataset,
    validate_features,
)
from backend.taste_visual import (
    FEATURE_AVATAR,
    FEATURE_COVER,
    FEATURE_MODE,
    taste_visual_features,
)


def comparison(row_id, left, right, choice):
    return SimpleNamespace(
        id=row_id,
        left_model_id=left,
        right_model_id=right,
        choice=choice,
    )


class TasteModelAlgorithmTests(unittest.TestCase):
    def test_dataset_keeps_skip_seen_but_not_labeled(self):
        dataset = build_label_dataset([
            comparison(1, 1, 2, "left"),
            comparison(2, 3, 4, "neither"),
            comparison(3, 5, 6, "skip"),
        ])

        self.assertEqual(dataset.labels, {1: 1, 2: 0, 3: 0, 4: 0})
        self.assertEqual(dataset.seen_model_ids, (1, 2, 3, 4, 5, 6))
        self.assertEqual(dataset.positive_count, 1)
        self.assertEqual(dataset.negative_count, 3)
        self.assertEqual(dataset.decisive_pair_count, 1)
        self.assertEqual(len(dataset.comparison_hash), 64)

    def test_duplicate_model_is_rejected_to_prevent_validation_leakage(self):
        with self.assertRaises(TasteModelUnavailable):
            build_label_dataset([
                comparison(1, 1, 2, "left"),
                comparison(2, 1, 3, "right"),
            ])

    def test_leave_pair_out_validation_passes_separable_features(self):
        rows = []
        features = {}
        for index in range(10):
            left = index * 2 + 1
            right = left + 1
            rows.append(comparison(index + 1, left, right, "left"))
            features[left] = np.asarray([1.0, index / 100], dtype=np.float32)
            features[right] = np.asarray([-1.0, index / 100], dtype=np.float32)
        dataset = build_label_dataset(rows)

        metrics = validate_features(dataset, features, apply_gate=True)

        self.assertTrue(metrics["passed"])
        self.assertEqual(metrics["auc"], 1.0)
        self.assertEqual(metrics["pair_accuracy"], 1.0)
        self.assertEqual(metrics["pair_correct"], 10)

    def test_quality_gate_rejects_uninformative_features(self):
        rows = []
        features = {}
        for index in range(10):
            left = index * 2 + 1
            right = left + 1
            rows.append(comparison(index + 1, left, right, "left"))
            features[left] = np.ones(4, dtype=np.float32)
            features[right] = np.ones(4, dtype=np.float32)
        dataset = build_label_dataset(rows)

        metrics = validate_features(dataset, features, apply_gate=True)

        self.assertFalse(metrics["passed"])
        self.assertEqual(metrics["auc"], 0.5)
        self.assertEqual(metrics["pair_accuracy"], 0.0)


class TasteModelPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = (Path(self.temp.name) / "taste-model.db").as_posix()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_result_feedback_replaces_active_value_without_tasks(self):
        async with self.session() as session:
            model = Model(
                ph_id="feedback-model",
                profile_type="model",
                profile_slug="feedback-model",
                name="Feedback Model",
                url="https://example/feedback-model",
            )
            hidden_model = Model(
                ph_id="hidden-feedback-model",
                profile_type="model",
                profile_slug="hidden-feedback-model",
                name="Hidden Feedback Model",
                url="https://example/hidden-feedback-model",
            )
            session.add_all([model, hidden_model])
            await session.flush()
            run = TasteModelRun(
                comparison_hash=build_label_dataset([]).comparison_hash,
                model_version="test-version",
                status="ready",
                quality_passed=True,
                published=True,
            )
            session.add(run)
            await session.flush()
            session.add(TasteModelScore(
                run_id=run.id, model_id=model.id, rank=1, score=0.5))
            session.add(TasteModelScore(
                run_id=run.id, model_id=hidden_model.id, rank=97, score=0.1))
            await session.commit()
            model_id = model.id
            hidden_model_id = hidden_model.id
            run_id = run.id

        service = TasteModelService()
        first = await service.record_feedback(
            model_id, "like", session_factory=self.session)
        second = await service.record_feedback(
            model_id, "dislike", session_factory=self.session)
        with self.assertRaises(TasteModelUnavailable):
            await service.record_feedback(
                hidden_model_id, "like", session_factory=self.session)

        self.assertEqual(first["feedback"], "like")
        self.assertEqual(second["feedback"], "dislike")
        async with self.session() as session:
            active = (await session.execute(select(TasteResultFeedback).where(
                TasteResultFeedback.active.is_(True)
            ))).scalars().all()
            feedback_count = (await session.execute(
                select(func.count()).select_from(TasteResultFeedback)
            )).scalar_one()
            task_count = (await session.execute(
                select(func.count()).select_from(Task)
            )).scalar_one()
        self.assertEqual(feedback_count, 2)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].action, "dislike")
        self.assertEqual(task_count, 0)
        snapshot = await service._feedback_dataset(session_factory=self.session)
        self.assertEqual(snapshot.labels, {model_id: 0})
        self.assertEqual(snapshot.positive_count, 0)
        self.assertEqual(snapshot.negative_count, 1)
        self.assertEqual(len(snapshot.signature), 64)
        with patch.object(taste_visual_features, "status", return_value={
            "available": True,
            "error": "",
            "model_version": "test-version",
        }):
            stale_state = await service.get_state(session_factory=self.session)
        self.assertTrue(stale_state["results_stale"])

        async with self.session() as session:
            stored_run = await session.get(TasteModelRun, run_id)
            stored_run.metrics_json = json.dumps({
                "result_feedback": {"signature": snapshot.signature},
            })
            await session.commit()
        with patch.object(taste_visual_features, "status", return_value={
            "available": True,
            "error": "",
            "model_version": "test-version",
        }):
            current_state = await service.get_state(session_factory=self.session)
        self.assertFalse(current_state["results_stale"])

    async def test_liked_feedback_returns_only_active_likes_in_latest_order(self):
        now = datetime.now()
        async with self.session() as session:
            models = [
                Model(
                    ph_id=f"liked-model-{index}",
                    profile_type="model",
                    profile_slug=f"liked-model-{index}",
                    name=f"Liked Model {index}",
                    url=f"https://example/liked-model-{index}",
                )
                for index in range(1, 5)
            ]
            session.add_all(models)
            await session.flush()
            run = TasteModelRun(
                comparison_hash=build_label_dataset([]).comparison_hash,
                model_version="test-version",
                status="ready",
                quality_passed=True,
                published=True,
            )
            session.add(run)
            await session.flush()
            session.add_all([
                TasteResultFeedback(
                    model_id=models[0].id,
                    run_id=run.id,
                    action="like",
                    active=True,
                    created_at=now - timedelta(minutes=4),
                ),
                # 防御性覆盖：即使旧库意外保留了同一模特的两个 active like，
                # 工作区也只能出现一次，并保留较新的记录。
                TasteResultFeedback(
                    model_id=models[0].id,
                    run_id=run.id,
                    action="like",
                    active=True,
                    created_at=now - timedelta(minutes=1),
                ),
                TasteResultFeedback(
                    model_id=models[1].id,
                    run_id=run.id,
                    action="like",
                    active=True,
                    created_at=now - timedelta(minutes=2),
                ),
                TasteResultFeedback(
                    model_id=models[2].id,
                    run_id=run.id,
                    action="like",
                    active=False,
                    created_at=now,
                ),
                TasteResultFeedback(
                    model_id=models[3].id,
                    run_id=run.id,
                    action="dislike",
                    active=True,
                    created_at=now + timedelta(minutes=1),
                ),
            ])
            await session.commit()
            expected_ids = [models[0].id, models[1].id]
            run_id = run.id

        service = TasteModelService()
        records = await service.liked_feedback(session_factory=self.session)

        self.assertEqual(
            [record["model_id"] for record in records], expected_ids)
        self.assertTrue(all(record["run_id"] == run_id for record in records))
        self.assertTrue(all(record["liked_at"] for record in records))
        self.assertEqual(
            records[0]["liked_at"], (now - timedelta(minutes=1)).isoformat())
        async with self.session() as session:
            task_count = (await session.execute(
                select(func.count()).select_from(Task)
            )).scalar_one()
        self.assertEqual(task_count, 0)

    async def test_feature_loader_uses_fixed_half_avatar_half_cover(self):
        avatar = np.asarray([1.0, 0.0], dtype="<f4")
        cover = np.asarray([0.0, 1.0], dtype="<f4")
        async with self.session() as session:
            model = Model(
                ph_id="feature-model",
                profile_type="model",
                profile_slug="feature-model",
                name="Feature Model",
                url="https://example/feature-model",
            )
            session.add(model)
            await session.flush()
            session.add_all([
                FeatureVector(
                    model_id=model.id,
                    feature_type=FEATURE_AVATAR,
                    item_key="avatar",
                    model_version="test-version",
                    input_hash="a" * 64,
                    dimensions=2,
                    vector=avatar.tobytes(),
                    status="ready",
                ),
                FeatureVector(
                    model_id=model.id,
                    feature_type=FEATURE_COVER,
                    item_key="first_cover",
                    model_version="test-version",
                    input_hash="b" * 64,
                    dimensions=2,
                    vector=cover.tobytes(),
                    status="ready",
                ),
            ])
            await session.commit()
            model_id = model.id

        with patch("backend.taste_visual.load_visual_manifest", return_value={
            "version": "test-version",
        }):
            matrices = await taste_visual_features.load_feature_matrices(
                [model_id], session_factory=self.session)

        np.testing.assert_allclose(
            matrices[FEATURE_MODE][model_id],
            np.asarray([2 ** -0.5, 2 ** -0.5], dtype=np.float32),
            rtol=1e-5,
        )

    async def test_publish_revalidation_uses_narrow_current_eligibility_gate(self):
        names = ("Valid", "Pending Avatar", "No Preview", "Excluded", "Feedback", "Library")
        async with self.session() as session:
            models = [Model(
                ph_id=f"gate-{index}", profile_type="model",
                profile_slug=f"gate-{index}", name=name,
                url=f"https://example/gate-{index}", avatar_status="ready",
                avatar_sha256=str(index) * 64,
            ) for index, name in enumerate(names, start=1)]
            session.add_all(models)
            await session.flush()
            models[1].avatar_status = "pending"
            for model in (models[0], models[1], models[3], models[4], models[5]):
                session.add(ModelPreviewVideo(
                    model_id=model.id, ordinal=0,
                    viewkey=f"gate{model.id:09d}", title=model.name,
                    url=f"https://example/video/{model.id}",
                    thumbnail=f"https://example/image/{model.id}.jpg",
                ))
            session.add(Candidate(
                candidate_key="excluded-gate", display_name=models[3].name,
                normalized_name="excluded", model_id=models[3].id,
                gender_status="excluded_non_performer",
            ))
            session.add(TasteResultFeedback(
                model_id=models[4].id, action="like", active=True))
            session.add(CloudModelFolder(
                cid="gate-library", name=models[5].name,
                normalized_name="library", model_id=models[5].id,
                match_method="manual", active=True,
            ))
            await session.commit()
            all_ids = [model.id for model in models]
            valid_id = models[0].id

        from backend.library_index import library_index
        library_index._invalidate_membership()
        service = TasteModelService()
        current = await service._revalidate_candidate_ids(
            all_ids, build_label_dataset([]), session_factory=self.session)
        self.assertEqual(current, [valid_id])

    async def test_score_storage_keeps_top_24_and_latest_three_runs(self):
        async with self.session() as session:
            models = [Model(
                ph_id=f"score-{index}", profile_type="model",
                name=f"Score {index}", url=f"https://example/score-{index}",
            ) for index in range(30)]
            session.add_all(models)
            await session.flush()
            runs = [TasteModelRun(
                comparison_hash=f"hash-{index}", model_version="test-version",
                status="validated", eligible_count=30,
            ) for index in range(4)]
            session.add_all(runs)
            await session.flush()
            session.add_all([
                TasteModelScore(
                    run_id=run.id, model_id=models[index].id,
                    rank=1, score=float(index),
                )
                for index, run in enumerate(runs[:3])
            ])
            await session.commit()
            latest_run_id = runs[-1].id
            oldest_run_id = runs[0].id
            ranked = [
                (model.id, float(30 - index))
                for index, model in enumerate(models)
            ]

        service = TasteModelService()
        await service._publish_scores(
            latest_run_id, ranked, {"formal": {"passed": True}},
            session_factory=self.session)

        async with self.session() as session:
            latest_count = (await session.execute(select(func.count()).select_from(
                TasteModelScore).where(
                    TasteModelScore.run_id == latest_run_id))).scalar_one()
            oldest_count = (await session.execute(select(func.count()).select_from(
                TasteModelScore).where(
                    TasteModelScore.run_id == oldest_run_id))).scalar_one()
            latest = await session.get(TasteModelRun, latest_run_id)
        self.assertEqual(latest_count, 24)
        self.assertEqual(oldest_count, 0)
        self.assertEqual(latest.scored_count, 30)


if __name__ == "__main__":
    unittest.main()
