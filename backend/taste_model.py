"""审美推荐的可验证视觉排序服务。

默认的收藏模式不需要手动选择：以 115 里已收藏的模特为正例、未收藏候选为背景，
取“收藏相对候选池”的视觉方向排序，并用历史“符合/不符合”评价留出检验后才发布。
早期的比较模式（两两比较 -> 模特级留组验证 -> 门禁 -> 排行）仍保留供脚本与测试使用。
服务只读模特/封面并写本地特征、验证代次与反馈，不调用任何同步或任务接口。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import time
from typing import Iterable

import numpy as np
from sqlalchemy import delete, exists, func, select, update

from . import avatar_store
from .database import (
    Candidate,
    Model,
    ModelPreviewVideo,
    TasteComparison,
    TasteModelRun,
    TasteModelScore,
    TasteResultFeedback,
    async_session,
)
from .library_index import library_index
from .preview_service import previews_by_model
from .taste_visual import (
    FEATURE_MODE,
    VisualModelUnavailable,
    taste_visual_features,
)


MIN_AUC = 0.65
MIN_PAIR_ACCURACY = 0.65
MIN_POSITIVE_MODELS = 10
MIN_DECISIVE_PAIRS = 10
MIN_CANDIDATE_COVERAGE = 0.80
RESULT_LIMIT = 24
# 命中率历史只展示最近几批，足够看出推荐是否在变好。
HISTORY_LIMIT = 8
RUNNING_STATUSES = frozenset({
    "validating", "fetching_covers", "indexing", "scoring",
})
FEEDBACK_ACTIONS = frozenset({"like", "dislike"})

# 收藏模式：以 115 收藏为正例，不需要手动比较。
RANKING_LIBRARY = "library_contrast"
LIBRARY_RUN_PREFIX = "library:"
# 收藏里至少这么多位有完整头像与首封面特征，才足以代表你的审美。
MIN_LIBRARY_POSITIVES = 20
# 用历史评价检验时两类各至少这么多位；不足时照常展示，但标为“尚未检验”。
MIN_FEEDBACK_PER_CLASS = 8
# 收藏模式每批保存前 96 位，页面每 24 位一组“换一批”。
RESULT_POOL = 96
# 收藏模式结果超过一天自动刷新，纳入新发现的候选。
LIBRARY_REFRESH_SECONDS = 24 * 3600
# 自动生成失败后，这段时间内打开页面不再自动重试，避免反复失败。
AUTO_RETRY_COOLDOWN_SECONDS = 30 * 60


class TasteModelUnavailable(RuntimeError):
    """当前标签或视觉资产不足以运行可验证排序。"""


@dataclass(frozen=True)
class LabeledPair:
    comparison_id: int
    left_model_id: int
    right_model_id: int
    choice: str


@dataclass(frozen=True)
class LabelDataset:
    pairs: tuple[LabeledPair, ...]
    labels: dict[int, int]
    seen_model_ids: tuple[int, ...]
    comparison_hash: str

    @property
    def model_ids(self) -> list[int]:
        return sorted(self.labels)

    @property
    def positive_count(self) -> int:
        return sum(value == 1 for value in self.labels.values())

    @property
    def negative_count(self) -> int:
        return sum(value == 0 for value in self.labels.values())

    @property
    def decisive_pair_count(self) -> int:
        return sum(pair.choice in {"left", "right"} for pair in self.pairs)


@dataclass(frozen=True)
class ResultFeedbackDataset:
    """当前生效的结果反馈快照；只参与门禁通过后的最终训练方向。"""

    labels: dict[int, int]
    signature: str

    @property
    def model_ids(self) -> list[int]:
        return sorted(self.labels)

    @property
    def positive_count(self) -> int:
        return sum(value == 1 for value in self.labels.values())

    @property
    def negative_count(self) -> int:
        return sum(value == 0 for value in self.labels.values())


def _choice_labels(pair: LabeledPair) -> tuple[int, int] | None:
    if pair.choice == "left":
        return 1, 0
    if pair.choice == "right":
        return 0, 1
    if pair.choice == "both":
        return 1, 1
    if pair.choice == "neither":
        return 0, 0
    return None


def build_label_dataset(rows: Iterable) -> LabelDataset:
    """把比较事件转换为唯一模特标签；重复或冲突会拒绝验证，避免特征泄漏。"""
    pairs: list[LabeledPair] = []
    labels: dict[int, int] = {}
    seen_model_ids: set[int] = set()
    canonical: list[dict] = []
    for row in rows:
        pair = LabeledPair(
            comparison_id=int(row.id),
            left_model_id=int(row.left_model_id),
            right_model_id=int(row.right_model_id),
            choice=str(row.choice),
        )
        canonical.append({
            "id": pair.comparison_id,
            "left": pair.left_model_id,
            "right": pair.right_model_id,
            "choice": pair.choice,
        })
        seen_model_ids.update((pair.left_model_id, pair.right_model_id))
        assigned = _choice_labels(pair)
        if assigned is None:
            continue
        for model_id, label in zip(
            (pair.left_model_id, pair.right_model_id), assigned,
        ):
            if model_id in labels:
                raise TasteModelUnavailable(
                    "同一模特出现在多组有效比较中；当前留组验证拒绝特征泄漏")
            labels[model_id] = label
        pairs.append(pair)
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return LabelDataset(
        pairs=tuple(pairs),
        labels=labels,
        seen_model_ids=tuple(sorted(seen_model_ids)),
        comparison_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def _normalize(vector: np.ndarray) -> np.ndarray:
    value = np.asarray(vector, dtype=np.float32)
    return value / max(float(np.linalg.norm(value)), 1e-12)


def _balanced_direction(
    features: dict[int, np.ndarray], labels: dict[int, int], train_ids: set[int],
) -> np.ndarray:
    positive = [features[model_id] for model_id in train_ids if labels[model_id] == 1]
    negative = [features[model_id] for model_id in train_ids if labels[model_id] == 0]
    if len(positive) < 2 or len(negative) < 2:
        raise TasteModelUnavailable("留组后正负样本不足，无法训练平衡视觉方向")
    positive_center = _normalize(np.mean(positive, axis=0))
    negative_center = _normalize(np.mean(negative, axis=0))
    return _normalize(positive_center - negative_center)


def library_direction(
    positives: Iterable[np.ndarray], background: Iterable[np.ndarray],
) -> np.ndarray:
    """“收藏相对候选池”的方向：收藏平均特征减去候选平均特征。"""
    positive = list(positives)
    others = list(background)
    if len(positive) < 2 or len(others) < 2:
        raise TasteModelUnavailable("收藏或候选的视觉特征不足，无法计算推荐方向")
    return _normalize(np.mean(positive, axis=0) - np.mean(others, axis=0))


def validate_library_ranking(
    positives: dict[int, np.ndarray],
    background: list[np.ndarray],
    labels: dict[int, int],
    label_features: dict[int, np.ndarray],
    *,
    apply_gate: bool,
) -> dict:
    """用历史“符合/不符合”检验收藏方向；评价过的模特不参与计算方向，避免自己考自己。"""
    usable = {model_id: label for model_id, label in labels.items()
              if model_id in label_features}
    liked = sum(label == 1 for label in usable.values())
    disliked = len(usable) - liked
    summary = {
        "method": "library_feedback_holdout",
        "labeled_models": len(usable),
        "positive_models": liked,
        "negative_models": disliked,
        "min_per_class": MIN_FEEDBACK_PER_CLASS,
    }
    if liked < MIN_FEEDBACK_PER_CLASS or disliked < MIN_FEEDBACK_PER_CLASS:
        return {**summary, "auc": None, "validated": False, "passed": None}
    direction = library_direction(
        (vector for model_id, vector in positives.items() if model_id not in usable),
        background,
    )
    scored = [(label, float(label_features[model_id] @ direction))
              for model_id, label in usable.items()]
    auc = _roc_auc(scored)
    top = sorted(scored, key=lambda item: -item[1])[:max(1, len(scored) * 3 // 10)]
    return {
        **summary,
        "auc": round(float(auc), 4),
        "top30_like_rate": round(sum(label for label, _score in top) / len(top), 4),
        "base_like_rate": round(liked / len(usable), 4),
        "validated": True,
        "passed": auc >= MIN_AUC if apply_gate else None,
    }


def _seen_ids(value) -> tuple[int, ...]:
    """接受比较数据集或直接的模特 ID 集合，返回需要排除的已看过模特。"""
    return tuple(getattr(value, "seen_model_ids", value) or ())


def _roc_auc(labels_and_scores: list[tuple[int, float]]) -> float:
    positive = [score for label, score in labels_and_scores if label == 1]
    negative = [score for label, score in labels_and_scores if label == 0]
    if not positive or not negative:
        raise TasteModelUnavailable("AUC 需要同时存在正负样本")
    wins = 0.0
    for positive_score in positive:
        for negative_score in negative:
            if positive_score > negative_score:
                wins += 1.0
            elif positive_score == negative_score:
                wins += 0.5
    return wins / (len(positive) * len(negative))


def _wilson_interval(successes: int, total: int) -> tuple[float, float]:
    if total <= 0:
        return 0.0, 0.0
    z = 1.96
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    ) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def validate_features(
    dataset: LabelDataset,
    features: dict[int, np.ndarray],
    *,
    apply_gate: bool,
) -> dict:
    """每次完整留出一组两位模特，训练集和验证组之间没有身份重叠。"""
    missing = sorted(set(dataset.labels) - set(features))
    if missing:
        raise TasteModelUnavailable(
            f"{len(missing)} 位已标注模特缺少完整头像/首封面特征")
    if dataset.positive_count < MIN_POSITIVE_MODELS:
        raise TasteModelUnavailable(
            f"明确正样本只有 {dataset.positive_count} 位，至少需要 {MIN_POSITIVE_MODELS} 位")
    if dataset.decisive_pair_count < MIN_DECISIVE_PAIRS:
        raise TasteModelUnavailable(
            f"明确胜负比较只有 {dataset.decisive_pair_count} 组，至少需要 {MIN_DECISIVE_PAIRS} 组")

    all_ids = set(dataset.labels)
    out_of_fold: dict[int, float] = {}
    pair_correct = 0
    pair_total = 0
    for pair in dataset.pairs:
        held_out = {pair.left_model_id, pair.right_model_id}
        direction = _balanced_direction(
            features, dataset.labels, all_ids - held_out)
        left_score = float(features[pair.left_model_id] @ direction)
        right_score = float(features[pair.right_model_id] @ direction)
        out_of_fold[pair.left_model_id] = left_score
        out_of_fold[pair.right_model_id] = right_score
        if pair.choice in {"left", "right"}:
            pair_total += 1
            winner_score, loser_score = (
                (left_score, right_score)
                if pair.choice == "left" else (right_score, left_score)
            )
            pair_correct += int(winner_score > loser_score)

    auc = _roc_auc([
        (dataset.labels[model_id], out_of_fold[model_id])
        for model_id in dataset.model_ids
    ])
    pair_accuracy = pair_correct / pair_total
    interval_low, interval_high = _wilson_interval(pair_correct, pair_total)
    passed = (
        auc >= MIN_AUC and pair_accuracy >= MIN_PAIR_ACCURACY
        if apply_gate else None
    )
    return {
        "auc": round(float(auc), 4),
        "pair_accuracy": round(float(pair_accuracy), 4),
        "pair_correct": pair_correct,
        "pair_total": pair_total,
        "pair_accuracy_ci95": [
            round(interval_low, 4), round(interval_high, 4),
        ],
        "positive_models": dataset.positive_count,
        "negative_models": dataset.negative_count,
        "labeled_models": len(dataset.labels),
        "passed": passed,
    }


class TasteModelService:
    def __init__(self):
        self._run_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._broadcast = None
        self._last_progress_write = 0.0
        # “最像你喜欢的谁”只依赖本批结果与当前正样本；两者不变时复用。
        self._explanation_key: tuple | None = None
        self._explanation_cache: dict[int, tuple[int, float]] = {}

    def set_broadcast(self, callback):
        self._broadcast = callback

    async def _emit(self, payload: dict):
        if self._broadcast is not None:
            await self._broadcast({"type": "taste_model_progress", **payload})

    async def close(self):
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await taste_visual_features.close()

    async def _dataset(self, *, session_factory) -> LabelDataset:
        async with session_factory() as session:
            rows = (await session.execute(select(TasteComparison).order_by(
                TasteComparison.id.asc()
            ))).scalars().all()
        return build_label_dataset(rows)

    async def _feedback_dataset(self, *, session_factory) -> ResultFeedbackDataset:
        async with session_factory() as session:
            rows = (await session.execute(select(
                TasteResultFeedback.model_id,
                TasteResultFeedback.action,
            ).where(
                TasteResultFeedback.active.is_(True),
            ).order_by(
                TasteResultFeedback.model_id.asc(),
                TasteResultFeedback.id.asc(),
            ))).all()
        labels: dict[int, int] = {}
        for row in rows:
            action = str(row.action)
            if action in FEEDBACK_ACTIONS:
                labels[int(row.model_id)] = int(action == "like")
        canonical = sorted(labels.items())
        encoded = json.dumps(canonical, separators=(",", ":"))
        return ResultFeedbackDataset(
            labels=labels,
            signature=hashlib.sha256(encoded.encode("ascii")).hexdigest(),
        )

    async def _eligible_candidates(
        self, dataset: LabelDataset | Iterable[int], *, session_factory,
    ) -> list[Model]:
        async with session_factory() as session:
            models = (await session.execute(select(Model).where(
                Model.profile_type.in_(("model", "pornstar")),
                Model.avatar_status == "ready",
                Model.avatar_sha256 != "",
                Model.id.not_in(_seen_ids(dataset)),
                exists().where(ModelPreviewVideo.model_id == Model.id),
                ~exists().where(
                    Candidate.model_id == Model.id,
                    Candidate.gender_status == "excluded_non_performer",
                ),
                ~exists().where(
                    TasteResultFeedback.model_id == Model.id,
                    TasteResultFeedback.active.is_(True),
                ),
            ).order_by(Model.id.asc()))).scalars().all()
            membership = await library_index.membership(session=session)
        candidates = [model for model in models if not membership.contains(model)]

        def available_ids(batch: list[tuple[int, str]]) -> set[int]:
            return {model_id for model_id, digest in batch
                    if avatar_store.avatar_path(digest) is not None}

        # 与校准页一样，把文件检查移出事件循环；每轮仍检查当前资产，
        # 不使用过期缓存替代生成资格，ORM 对象也不交给工作线程。
        available: set[int] = set()
        for start in range(0, len(candidates), 128):
            batch = [(model.id, model.avatar_sha256)
                     for model in candidates[start:start + 128]]
            available.update(await asyncio.to_thread(available_ids, batch))
        return [model for model in candidates if model.id in available]

    async def _revalidate_candidate_ids(
        self,
        candidate_ids: Iterable[int],
        dataset: LabelDataset | Iterable[int],
        *,
        session_factory,
    ) -> list[int]:
        """发布前只对本轮 ID 复核所有易变门禁，不重复全库/文件扫描。"""
        ids = sorted({int(model_id) for model_id in candidate_ids
                      if int(model_id) > 0})
        if not ids:
            return []
        async with session_factory() as session:
            current_candidates = (await session.execute(select(Model).where(
                Model.id.in_(ids),
                Model.profile_type.in_(("model", "pornstar")),
                Model.avatar_status == "ready",
                Model.avatar_sha256 != "",
                Model.id.not_in(_seen_ids(dataset)),
                exists().where(ModelPreviewVideo.model_id == Model.id),
                ~exists().where(
                    Candidate.model_id == Model.id,
                    Candidate.gender_status == "excluded_non_performer",
                ),
                ~exists().where(
                    TasteResultFeedback.model_id == Model.id,
                    TasteResultFeedback.active.is_(True),
                ),
            ).order_by(Model.id.asc()))).scalars().all()
            membership = await library_index.membership(session=session)
        return [
            int(model.id) for model in current_candidates
            if not membership.contains(model)
        ]

    async def _create_run(
        self, dataset: LabelDataset, model_version: str, *, session_factory,
    ) -> int:
        async with session_factory() as session:
            run = TasteModelRun(
                comparison_hash=dataset.comparison_hash,
                model_version=model_version,
                feature_mode=FEATURE_MODE,
                status="validating",
                progress_current=0,
                progress_total=len(dataset.labels),
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return int(run.id)

    async def _create_library_run(
        self, signature: str, model_version: str, *, session_factory,
    ) -> int:
        async with session_factory() as session:
            run = TasteModelRun(
                comparison_hash=LIBRARY_RUN_PREFIX + signature,
                model_version=model_version,
                feature_mode=FEATURE_MODE,
                status="validating",
                progress_current=0,
                progress_total=0,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return int(run.id)

    async def _library_snapshot(self, *, session_factory) -> tuple[list[int], str]:
        """当前 115 收藏的模特 ID 与签名；签名变化说明收藏有增减，推荐需要刷新。"""
        async with session_factory() as session:
            rows = (await session.execute(
                select(Model.id, Model.name, Model.cloud_cid))).all()
            membership = await library_index.membership(session=session)
        ids = sorted(int(row.id) for row in rows if membership.contains(row))
        signature = hashlib.sha256(
            json.dumps(ids, separators=(",", ":")).encode("ascii")).hexdigest()
        return ids, signature

    async def _comparison_model_ids(self, *, session_factory) -> set[int]:
        """比较页里出现过的模特；收藏模式同样不再推荐，且不因比较记录冲突而失败。"""
        async with session_factory() as session:
            rows = (await session.execute(select(
                TasteComparison.left_model_id, TasteComparison.right_model_id,
            ))).all()
        return {int(value) for row in rows for value in row if value}

    async def _update_run(self, run_id: int, *, session_factory, **fields):
        async with session_factory() as session:
            await session.execute(update(TasteModelRun).where(
                TasteModelRun.id == run_id
            ).values(**fields))
            await session.commit()

    async def _progress(
        self,
        run_id: int,
        phase: str,
        current: int,
        total: int,
        *,
        session_factory,
    ):
        now = time.monotonic()
        if current < total and now - self._last_progress_write < 1.0:
            return
        self._last_progress_write = now
        await self._update_run(
            run_id,
            session_factory=session_factory,
            status=phase,
            progress_current=max(0, int(current)),
            progress_total=max(0, int(total)),
        )
        await self._emit({
            "run_id": run_id,
            "status": phase,
            "progress_current": max(0, int(current)),
            "progress_total": max(0, int(total)),
        })

    async def _publish_scores(
        self,
        run_id: int,
        ranked: list[tuple[int, float]],
        metrics: dict,
        *,
        session_factory,
        limit: int = RESULT_LIMIT,
    ):
        now = datetime.now()
        async with session_factory() as session:
            await session.execute(delete(TasteModelScore).where(
                TasteModelScore.run_id == run_id))
            session.add_all([
                TasteModelScore(
                    run_id=run_id,
                    model_id=model_id,
                    rank=rank,
                    score=float(score),
                    created_at=now,
                )
                for rank, (model_id, score) in enumerate(
                    ranked[:limit], start=1)
            ])
            retained_runs = select(TasteModelRun.id).order_by(
                TasteModelRun.id.desc()).limit(3)
            await session.execute(delete(TasteModelScore).where(
                TasteModelScore.run_id.not_in(retained_runs)
            ))
            await session.execute(update(TasteModelRun).where(
                TasteModelRun.published.is_(True)
            ).values(published=False))
            run = await session.get(TasteModelRun, run_id)
            if run is None:
                raise RuntimeError("审美视觉代次在发布前丢失")
            run.status = "ready"
            run.quality_passed = True
            run.published = True
            run.progress_current = len(ranked)
            run.progress_total = len(ranked)
            run.scored_count = len(ranked)
            run.excluded_count = max(0, run.eligible_count - len(ranked))
            run.metrics_json = json.dumps(metrics, ensure_ascii=False)
            run.error = ""
            run.completed_at = now
            await session.commit()

    async def _validate_dataset(
        self,
        run_id: int,
        dataset: LabelDataset,
        *,
        session_factory,
    ) -> tuple[dict, dict[str, dict[int, np.ndarray]]]:
        async def label_progress(_phase: str, current: int, total: int):
            await self._progress(
                run_id, "validating", current, total,
                session_factory=session_factory)

        await taste_visual_features.index_models(
            dataset.model_ids,
            fetch_missing_covers=False,
            progress=label_progress,
            session_factory=session_factory,
        )
        matrices = await taste_visual_features.load_feature_matrices(
            dataset.model_ids, session_factory=session_factory)
        formal = validate_features(
            dataset, matrices[FEATURE_MODE], apply_gate=True)
        avatar_diagnostic = validate_features(
            dataset, matrices["avatar_only"], apply_gate=False)
        return ({
            "thresholds": {
                "auc": MIN_AUC,
                "pair_accuracy": MIN_PAIR_ACCURACY,
                "candidate_coverage": MIN_CANDIDATE_COVERAGE,
            },
            "formal": formal,
            "diagnostics": {"avatar_only": avatar_diagnostic},
            "feature_mode": FEATURE_MODE,
        }, matrices)

    async def _index_candidates(
        self,
        run_id: int,
        candidate_ids: list[int],
        metrics: dict,
        *,
        session_factory,
        **run_fields,
    ) -> bool:
        """补齐候选封面并本机计算特征；覆盖率不足时标记失败并返回 False。"""
        await self._update_run(
            run_id,
            session_factory=session_factory,
            status="fetching_covers",
            eligible_count=len(candidate_ids),
            metrics_json=json.dumps(metrics, ensure_ascii=False),
            progress_current=0,
            progress_total=len(candidate_ids),
            **run_fields,
        )

        async def candidate_progress(phase: str, current: int, total: int):
            await self._progress(
                run_id, phase, current, total,
                session_factory=session_factory)

        index_result = await taste_visual_features.index_models(
            candidate_ids,
            fetch_missing_covers=True,
            progress=candidate_progress,
            session_factory=session_factory,
            # 已有封面高于覆盖率门槛一定余量时，补齐只占有限时间，下一批更快出现。
            min_cover_ratio=min(1.0, MIN_CANDIDATE_COVERAGE + 0.05),
        )
        coverage = (
            index_result.ready_models / index_result.requested_models
            if index_result.requested_models else 0.0
        )
        metrics["candidate_index"] = {
            "eligible": index_result.requested_models,
            "ready": index_result.ready_models,
            "excluded": index_result.excluded_models,
            "coverage": round(coverage, 4),
            "cover_fetch_total": index_result.cover_fetch_total,
            "cover_fetch_succeeded": index_result.cover_fetch_succeeded,
            "cover_fetch_deferred": index_result.cover_fetch_deferred,
            "inferred_images": index_result.inferred_images,
            "cached_images": index_result.cached_images,
        }
        if coverage >= MIN_CANDIDATE_COVERAGE:
            return True
        await self._update_run(
            run_id,
            session_factory=session_factory,
            status="failed",
            scored_count=index_result.ready_models,
            excluded_count=index_result.excluded_models,
            metrics_json=json.dumps(metrics, ensure_ascii=False),
            error=(
                f"候选视觉覆盖率 {coverage:.1%} 低于 "
                f"{MIN_CANDIDATE_COVERAGE:.0%}，已拒绝发布偏置结果"
            ),
            completed_at=datetime.now(),
        )
        await self._emit({
            "run_id": run_id,
            "status": "failed",
            "error": (
                f"候选视觉覆盖率 {coverage:.1%} 低于 "
                f"{MIN_CANDIDATE_COVERAGE:.0%}"
            ),
        })
        return False

    async def _feedback_features(
        self, feedback_dataset: ResultFeedbackDataset, *, session_factory,
    ) -> dict[int, np.ndarray]:
        """历史评价模特的融合特征；缺项只增量补算（不抓新封面），仍缺的跳过。"""
        if not feedback_dataset.model_ids:
            return {}
        features = (await taste_visual_features.load_feature_matrices(
            feedback_dataset.model_ids, session_factory=session_factory))[FEATURE_MODE]
        missing = sorted(set(feedback_dataset.model_ids) - set(features))
        if missing:
            await taste_visual_features.index_models(
                missing, fetch_missing_covers=False, session_factory=session_factory)
            features.update((await taste_visual_features.load_feature_matrices(
                missing, session_factory=session_factory))[FEATURE_MODE])
        return features

    async def _execute_library(self, run_id: int, *, session_factory):
        """收藏模式：115 收藏为正例、未收藏候选为背景，历史评价检验通过后发布。"""
        try:
            library_ids, signature = await self._library_snapshot(
                session_factory=session_factory)

            async def library_progress(_phase: str, current: int, total: int):
                await self._progress(
                    run_id, "validating", current, total,
                    session_factory=session_factory)

            # 新收进 115 的模特在这里增量补算特征；已缓存的直接复用。
            await taste_visual_features.index_models(
                library_ids,
                fetch_missing_covers=True,
                progress=library_progress,
                session_factory=session_factory,
            )
            positives = (await taste_visual_features.load_feature_matrices(
                library_ids, session_factory=session_factory))[FEATURE_MODE]
            if len(positives) < MIN_LIBRARY_POSITIVES:
                raise TasteModelUnavailable(
                    f"115 收藏里只有 {len(positives)} 位有完整的头像和作品封面，"
                    f"至少需要 {MIN_LIBRARY_POSITIVES} 位才能代表你的审美")
            feedback_dataset = await self._feedback_dataset(
                session_factory=session_factory)
            label_features = await self._feedback_features(
                feedback_dataset, session_factory=session_factory)
            metrics: dict = {
                "ranking": RANKING_LIBRARY,
                "feature_mode": FEATURE_MODE,
                "thresholds": {
                    "auc": MIN_AUC,
                    "candidate_coverage": MIN_CANDIDATE_COVERAGE,
                    "feedback_per_class": MIN_FEEDBACK_PER_CLASS,
                },
                "library": {
                    "signature": signature,
                    "members": len(library_ids),
                    "positives": len(positives),
                },
            }

            seen = await self._comparison_model_ids(session_factory=session_factory)
            candidates = await self._eligible_candidates(
                seen, session_factory=session_factory)
            candidate_ids = [int(model.id) for model in candidates]
            if not await self._index_candidates(
                    run_id, candidate_ids, metrics, session_factory=session_factory):
                return
            current_ids = await self._revalidate_candidate_ids(
                candidate_ids, seen, session_factory=session_factory)
            background = (await taste_visual_features.load_feature_matrices(
                current_ids, session_factory=session_factory))[FEATURE_MODE]

            formal = validate_library_ranking(
                positives, list(background.values()), feedback_dataset.labels,
                label_features, apply_gate=True)
            metrics["formal"] = formal
            if formal["passed"] is False:
                await self._update_run(
                    run_id,
                    session_factory=session_factory,
                    status="quality_failed",
                    quality_passed=False,
                    metrics_json=json.dumps(metrics, ensure_ascii=False),
                    error="按 115 收藏排序在你过去的评价上区分度不足",
                    completed_at=datetime.now(),
                )
                await self._emit({"run_id": run_id, "status": "quality_failed"})
                return

            direction = library_direction(positives.values(), background.values())
            ranked = sorted(
                ((model_id, float(vector @ direction))
                 for model_id, vector in background.items()),
                key=lambda item: (-item[1], item[0]),
            )
            await self._update_run(
                run_id,
                session_factory=session_factory,
                status="scoring",
                progress_current=0,
                progress_total=len(ranked),
                eligible_count=len(current_ids),
            )
            await self._publish_scores(
                run_id, ranked, metrics,
                session_factory=session_factory, limit=RESULT_POOL)
            await self._emit({
                "run_id": run_id, "status": "ready", "scored_count": len(ranked),
            })
        except asyncio.CancelledError:
            await self._update_run(
                run_id,
                session_factory=session_factory,
                status="failed",
                error="后端关闭，生成已中断；可以安全重试",
                completed_at=datetime.now(),
            )
            raise
        except Exception as exc:
            await self._update_run(
                run_id,
                session_factory=session_factory,
                status="failed",
                error=str(exc)[:2000],
                completed_at=datetime.now(),
            )
            await self._emit({
                "run_id": run_id, "status": "failed", "error": str(exc)[:500],
            })

    async def _execute(
        self,
        run_id: int,
        dataset: LabelDataset,
        feedback_dataset: ResultFeedbackDataset,
        *,
        session_factory,
    ):
        try:
            metrics, matrices = await self._validate_dataset(
                run_id, dataset, session_factory=session_factory)
            formal = metrics["formal"]
            if not formal["passed"]:
                await self._update_run(
                    run_id,
                    session_factory=session_factory,
                    status="quality_failed",
                    quality_passed=False,
                    metrics_json=json.dumps(metrics, ensure_ascii=False),
                    error="视觉留组验证未达到发布门槛",
                    completed_at=datetime.now(),
                )
                await self._emit({"run_id": run_id, "status": "quality_failed"})
                return

            feedback_features: dict[int, np.ndarray] = {
                model_id: matrices[FEATURE_MODE][model_id]
                for model_id in feedback_dataset.model_ids
                if model_id in matrices[FEATURE_MODE]
            }
            if feedback_dataset.model_ids:
                # 反馈来自此前已评分的结果，正常情况下向量已经缓存；再次索引只会
                # 增量补齐模型升级或缓存清理后的缺项，不会抓视频或创建任务。
                missing_feedback_ids = sorted(
                    set(feedback_dataset.model_ids) - set(feedback_features))
                if missing_feedback_ids:
                    await taste_visual_features.index_models(
                        missing_feedback_ids,
                        fetch_missing_covers=False,
                        session_factory=session_factory,
                    )
                    feedback_matrices = await taste_visual_features.load_feature_matrices(
                        missing_feedback_ids, session_factory=session_factory)
                    feedback_features.update(feedback_matrices[FEATURE_MODE])
                missing_feedback = sorted(
                    set(feedback_dataset.model_ids) - set(feedback_features))
                if missing_feedback:
                    raise TasteModelUnavailable(
                        f"{len(missing_feedback)} 条结果反馈缺少当前版本视觉特征")

            metrics["result_feedback"] = {
                "signature": feedback_dataset.signature,
                "positive_models": feedback_dataset.positive_count,
                "negative_models": feedback_dataset.negative_count,
                "used_models": len(feedback_features),
            }

            candidates = await self._eligible_candidates(
                dataset, session_factory=session_factory)
            candidate_ids = [int(model.id) for model in candidates]
            if not await self._index_candidates(
                    run_id, candidate_ids, metrics,
                    session_factory=session_factory, quality_passed=True):
                return

            latest_dataset = await self._dataset(session_factory=session_factory)
            if latest_dataset.comparison_hash != dataset.comparison_hash:
                raise TasteModelUnavailable("生成期间比较记录发生变化，请重新验证")
            latest_feedback = await self._feedback_dataset(
                session_factory=session_factory)
            if latest_feedback.signature != feedback_dataset.signature:
                raise TasteModelUnavailable("生成期间结果反馈发生变化，请重新生成")

            # 长时间封面补齐期间，模特可能被删除、头像失效、被标记为非表演者、
            # 新增结果反馈或进入115。只对本轮候选 ID 做窄查询复核全部门禁，
            # 避免恢复全表扫描和数千次文件 stat，同时不发布失效外键/候选。
            current_ids = await self._revalidate_candidate_ids(
                (model.id for model in candidates), dataset,
                session_factory=session_factory)
            candidate_matrices = await taste_visual_features.load_feature_matrices(
                current_ids, session_factory=session_factory)
            training_features = dict(matrices[FEATURE_MODE])
            training_features.update(feedback_features)
            training_labels = dict(dataset.labels)
            training_labels.update(feedback_dataset.labels)
            train_direction = _balanced_direction(
                training_features, training_labels, set(training_labels))
            ranked = sorted(
                (
                    (model_id, float(vector @ train_direction))
                    for model_id, vector in candidate_matrices[FEATURE_MODE].items()
                ),
                key=lambda item: (-item[1], item[0]),
            )
            await self._update_run(
                run_id,
                session_factory=session_factory,
                status="scoring",
                progress_current=0,
                progress_total=len(ranked),
                eligible_count=len(current_ids),
            )
            await self._publish_scores(
                run_id, ranked, metrics, session_factory=session_factory)
            await self._emit({
                "run_id": run_id,
                "status": "ready",
                "scored_count": len(ranked),
            })
        except asyncio.CancelledError:
            await self._update_run(
                run_id,
                session_factory=session_factory,
                status="failed",
                error="后端关闭，生成已中断；可以安全重试",
                completed_at=datetime.now(),
            )
            raise
        except Exception as exc:
            await self._update_run(
                run_id,
                session_factory=session_factory,
                status="failed",
                error=str(exc)[:2000],
                completed_at=datetime.now(),
            )
            await self._emit({
                "run_id": run_id,
                "status": "failed",
                "error": str(exc)[:500],
            })

    async def validate_now(self, *, session_factory=None) -> dict:
        """只提取已标注样本并保存留组验证结果，不扫描剩余候选。"""
        factory = session_factory or async_session
        async with self._run_lock:
            dataset = await self._dataset(session_factory=factory)
            status = await asyncio.to_thread(taste_visual_features.status)
            if not status["available"]:
                raise VisualModelUnavailable(status["error"])
            run_id = await self._create_run(
                dataset, str(status["model_version"]), session_factory=factory)
            try:
                metrics, _matrices = await self._validate_dataset(
                    run_id, dataset, session_factory=factory)
                formal = metrics["formal"]
                await self._update_run(
                    run_id,
                    session_factory=factory,
                    status="validated" if formal["passed"] else "quality_failed",
                    quality_passed=bool(formal["passed"]),
                    metrics_json=json.dumps(metrics, ensure_ascii=False),
                    error="" if formal["passed"] else "视觉留组验证未达到发布门槛",
                    completed_at=datetime.now(),
                )
            except Exception as exc:
                await self._update_run(
                    run_id,
                    session_factory=factory,
                    status="failed",
                    error=str(exc)[:2000],
                    completed_at=datetime.now(),
                )
                raise
        return await self.get_state(session_factory=factory)

    async def run_now(self, *, session_factory=None, mode: str = "comparison") -> dict:
        """同步等待一次完整生成，供受控脚本与测试使用。"""
        factory = session_factory or async_session
        if mode == "library":
            async with self._run_lock:
                run_id = await self._start_library_run(session_factory=factory)
                await self._execute_library(run_id, session_factory=factory)
            return await self.get_state(session_factory=factory)
        async with self._run_lock:
            dataset = await self._dataset(session_factory=factory)
            feedback_dataset = await self._feedback_dataset(session_factory=factory)
            status = await asyncio.to_thread(taste_visual_features.status)
            if not status["available"]:
                raise VisualModelUnavailable(status["error"])
            run_id = await self._create_run(
                dataset, str(status["model_version"]), session_factory=factory)
            await self._execute(
                run_id, dataset, feedback_dataset, session_factory=factory)
        return await self.get_state(session_factory=factory)

    async def _start_library_run(self, *, session_factory) -> int:
        status = await asyncio.to_thread(taste_visual_features.status)
        if not status["available"]:
            raise VisualModelUnavailable(status["error"])
        _ids, signature = await self._library_snapshot(session_factory=session_factory)
        return await self._create_library_run(
            signature, str(status["model_version"]), session_factory=session_factory)

    async def start(self, *, session_factory=None, mode: str = "comparison") -> dict:
        factory = session_factory or async_session
        async with self._run_lock:
            if self._task is not None and not self._task.done():
                return await self.get_state(session_factory=factory)
            if mode == "library":
                run_id = await self._start_library_run(session_factory=factory)
                self._task = asyncio.create_task(
                    self._execute_library(run_id, session_factory=factory),
                    name=f"taste-library-run-{run_id}",
                )
                return await self.get_state(session_factory=factory)
            dataset = await self._dataset(session_factory=factory)
            feedback_dataset = await self._feedback_dataset(session_factory=factory)
            status = await asyncio.to_thread(taste_visual_features.status)
            if not status["available"]:
                raise VisualModelUnavailable(status["error"])
            run_id = await self._create_run(
                dataset, str(status["model_version"]), session_factory=factory)
            self._task = asyncio.create_task(
                self._execute(
                    run_id, dataset, feedback_dataset, session_factory=factory),
                name=f"taste-model-run-{run_id}",
            )
        return await self.get_state(session_factory=factory)

    @staticmethod
    def _run_wire(run: TasteModelRun | None) -> dict | None:
        if run is None:
            return None
        try:
            metrics = json.loads(run.metrics_json or "{}")
        except json.JSONDecodeError:
            metrics = {}
        return {
            "id": run.id,
            "status": run.status,
            "quality_passed": bool(run.quality_passed),
            "published": bool(run.published),
            "feature_mode": run.feature_mode,
            "model_version": run.model_version,
            "progress_current": int(run.progress_current or 0),
            "progress_total": int(run.progress_total or 0),
            "eligible_count": int(run.eligible_count or 0),
            "scored_count": int(run.scored_count or 0),
            "excluded_count": int(run.excluded_count or 0),
            "metrics": metrics,
            "error": run.error or "",
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "completed_at": (
                run.completed_at.isoformat() if run.completed_at else None),
        }

    async def _explanations(
        self,
        run_id: int,
        result_ids: list[int],
        positive_ids: set[int],
        *,
        session_factory,
    ) -> dict[int, tuple[int, float]]:
        """每个推荐与哪一位已标为喜欢的模特最像（同一套 50/50 视觉特征的余弦）。

        只做展示说明，不参与排序；特征缺失或本地视觉模型不可用时返回空，
        卡片退回通用说明。
        """
        key = (int(run_id), tuple(sorted(result_ids)), tuple(sorted(positive_ids)))
        if key == self._explanation_key:
            return self._explanation_cache
        explanations: dict[int, tuple[int, float]] = {}
        if result_ids and positive_ids:
            try:
                matrices = await taste_visual_features.load_feature_matrices(
                    sorted(set(result_ids) | set(positive_ids)),
                    session_factory=session_factory)
                vectors = matrices[FEATURE_MODE]
            except Exception:
                vectors = {}
            positives = [(model_id, vectors[model_id]) for model_id in sorted(positive_ids)
                         if model_id in vectors]
            for model_id in result_ids:
                vector = vectors.get(model_id)
                if vector is None:
                    continue
                best = max(
                    ((float(vector @ other), other_id)
                     for other_id, other in positives if other_id != model_id),
                    default=None,
                )
                if best is not None:
                    explanations[model_id] = (best[1], round(best[0], 3))
        self._explanation_key = key
        self._explanation_cache = explanations
        return explanations

    async def _results(
        self,
        run: TasteModelRun,
        *,
        session_factory,
        positive_ids: set[int] | None = None,
        positive_label: str = "喜欢",
    ) -> tuple[list[dict], int]:
        """当前批次的展示结果与因已进入 115 而隐藏的人数。"""
        async with session_factory() as session:
            scores = (await session.execute(select(TasteModelScore).where(
                TasteModelScore.run_id == run.id
            ).order_by(TasteModelScore.rank.asc()).limit(RESULT_POOL))).scalars().all()
            model_ids = [int(score.model_id) for score in scores]
            models = (await session.execute(select(Model).where(
                Model.id.in_(model_ids)
            ))).scalars().all() if model_ids else []
            feedback_rows = (await session.execute(select(TasteResultFeedback).where(
                TasteResultFeedback.model_id.in_(model_ids),
                TasteResultFeedback.active.is_(True),
            ).order_by(TasteResultFeedback.id.asc()))).scalars().all() if model_ids else []
            previews = await previews_by_model(session, model_ids)
            membership = await library_index.membership(session=session)
        by_id = {int(model.id): model for model in models}
        feedback = {int(row.model_id): row.action for row in feedback_rows}
        # 结果发布后才进入 115 的模特不再推荐；已经评价过的保留，方便回看。
        hidden = {
            model_id for model_id, model in by_id.items()
            if membership.contains(model) and model_id not in feedback
        }
        visible_ids = [model_id for model_id in model_ids
                       if model_id in by_id and model_id not in hidden]
        explanations = await self._explanations(
            run.id, visible_ids, set(positive_ids or ()), session_factory=session_factory)
        similar_ids = sorted({similar for similar, _ in explanations.values()})
        similar_models: dict[int, Model] = {}
        if similar_ids:
            async with session_factory() as session:
                similar_models = {int(model.id): model for model in (await session.execute(
                    select(Model).where(Model.id.in_(similar_ids))
                )).scalars().all()}
        result = []
        for score in scores:
            model = by_id.get(int(score.model_id))
            if model is None or model.id in hidden:
                continue
            public_slug = str(model.profile_slug or model.ph_id).split(":", 1)[-1]
            similar = explanations.get(int(model.id))
            similar_model = similar_models.get(similar[0]) if similar else None
            result.append({
                "rank": int(score.rank),
                "model": {
                    "id": model.id,
                    "ph_id": public_slug,
                    "profile_type": model.profile_type or "model",
                    "profile_slug": public_slug,
                    "name": model.name,
                    "url": model.url,
                    "avatar_url": (
                        f"/api/models/{model.id}/avatar?v="
                        f"{str(model.avatar_sha256)[:16]}"
                    ),
                    "preview_videos": previews.get(model.id, [])[:4],
                },
                "match_level": "优先查看" if score.rank <= 8 else "继续观察",
                "reason": (
                    f"头像和作品封面与你{positive_label}的 {similar_model.name} 最接近"
                    if similar_model is not None
                    else "按头像与首张作品封面的整体风格排序"
                ),
                "similar_to": ({
                    "id": similar_model.id,
                    "name": similar_model.name,
                    "avatar_url": (
                        f"/api/models/{similar_model.id}/avatar?v="
                        f"{str(similar_model.avatar_sha256)[:16]}"
                    ),
                    "similarity": similar[1],
                } if similar_model is not None else None),
                "similar_label": positive_label,
                "feedback": feedback.get(int(score.model_id)),
            })
        return result, len(hidden)

    @staticmethod
    async def _batch_ordinals(session) -> dict[int, int]:
        """成功发布过结果的代次按先后编号为“第 N 批”。"""
        ready_ids = (await session.execute(select(TasteModelRun.id).where(
            TasteModelRun.status == "ready",
        ).order_by(TasteModelRun.id.asc()))).scalars().all()
        return {int(run_id): index for index, run_id in enumerate(ready_ids, start=1)}

    async def _history(self, *, session_factory) -> list[dict]:
        """最近几批推荐的真实命中率：来自你对每批结果的“符合/不符合”。"""
        async with session_factory() as session:
            rows = (await session.execute(select(
                TasteResultFeedback.run_id,
                TasteResultFeedback.action,
                func.count(TasteResultFeedback.id),
            ).where(
                TasteResultFeedback.active.is_(True),
                TasteResultFeedback.run_id.is_not(None),
            ).group_by(TasteResultFeedback.run_id, TasteResultFeedback.action))).all()
            run_ids = sorted({int(run_id) for run_id, _action, _count in rows})
            completed = dict((await session.execute(select(
                TasteModelRun.id, TasteModelRun.completed_at,
            ).where(TasteModelRun.id.in_(run_ids)))).all()) if run_ids else {}
            ordinals = await self._batch_ordinals(session)
        counts: dict[int, dict[str, int]] = {}
        for run_id, action, count in rows:
            counts.setdefault(int(run_id), {"like": 0, "dislike": 0})[str(action)] = int(count)
        history = []
        for run_id in run_ids[-HISTORY_LIMIT:]:
            liked = counts[run_id].get("like", 0)
            disliked = counts[run_id].get("dislike", 0)
            rated = liked + disliked
            stamp = completed.get(run_id)
            history.append({
                "run_id": run_id,
                "batch": ordinals.get(run_id),
                "liked": liked,
                "disliked": disliked,
                "rated": rated,
                "like_rate": round(liked / rated, 4) if rated else None,
                "completed_at": stamp.isoformat() if stamp else None,
            })
        return history

    async def get_state(self, *, session_factory=None) -> dict:
        factory = session_factory or async_session
        dataset_error = ""
        try:
            dataset = await self._dataset(session_factory=factory)
        except TasteModelUnavailable as exc:
            dataset = None
            dataset_error = str(exc)
        feedback_dataset = await self._feedback_dataset(session_factory=factory)
        async with factory() as session:
            latest = (await session.execute(select(TasteModelRun).order_by(
                TasteModelRun.id.desc()
            ).limit(1))).scalar_one_or_none()
            published = (await session.execute(select(TasteModelRun).where(
                TasteModelRun.published.is_(True),
                TasteModelRun.status == "ready",
            ).order_by(TasteModelRun.id.desc()).limit(1))).scalar_one_or_none()

        latest_wire = self._run_wire(latest)
        library_ids, library_signature = await self._library_snapshot(
            session_factory=factory)
        published_library = bool(
            published and str(published.comparison_hash or "").startswith(LIBRARY_RUN_PREFIX))
        interrupted = bool(
            latest and latest.status in RUNNING_STATUSES
            and (self._task is None or self._task.done())
        )
        if interrupted and latest_wire:
            latest_wire["status"] = "interrupted"
            latest_wire["error"] = "后端已重启，上一次生成没有继续运行；可以安全重试"
        if published_library:
            # 收藏模式按“最像你收藏的谁”解释；比较模式沿用“最像你喜欢的谁”。
            positive_ids = set(library_ids)
            positive_label = "收藏"
        else:
            positive_ids = {
                model_id for model_id, label in (dataset.labels.items() if dataset else ())
                if label == 1
            } | {model_id for model_id, label in feedback_dataset.labels.items() if label == 1}
            positive_label = "喜欢"
        results, hidden_in_library = (
            await self._results(
                published, session_factory=factory, positive_ids=positive_ids,
                positive_label=positive_label)
            if published else ([], 0)
        )
        history = await self._history(session_factory=factory)
        batch_number = None
        if published:
            async with factory() as session:
                batch_number = (await self._batch_ordinals(session)).get(int(published.id))
        current_hash = dataset.comparison_hash if dataset else ""
        comparison_stale = bool(
            published and current_hash != published.comparison_hash)
        published_feedback_signature = ""
        if published:
            try:
                published_metrics = json.loads(published.metrics_json or "{}")
            except json.JSONDecodeError:
                published_metrics = {}
            published_feedback_signature = str(
                published_metrics.get("result_feedback", {}).get("signature", ""))
        feedback_stale = bool(
            published
            and (
                (published_feedback_signature
                 and published_feedback_signature != feedback_dataset.signature)
                or (not published_feedback_signature and feedback_dataset.labels)
            )
        )
        now = datetime.now()
        if published_library:
            # 收藏有增减，或结果超过一天（期间可能发现了新候选），都需要刷新。
            library_changed = (
                str(published.comparison_hash) != LIBRARY_RUN_PREFIX + library_signature)
            published_at = published.completed_at or published.created_at
            expired = bool(
                published_at
                and (now - published_at).total_seconds() >= LIBRARY_REFRESH_SECONDS)
            stale = library_changed or expired
        else:
            stale = comparison_stale or feedback_stale
        visual_status = await asyncio.to_thread(taste_visual_features.status)
        running = bool(self._task and not self._task.done())
        can_start = bool(visual_status["available"] and not running)
        failed_at = (
            latest.completed_at
            if latest and latest.status in {"failed", "quality_failed"} else None)
        recently_failed = bool(
            failed_at and (now - failed_at).total_seconds() < AUTO_RETRY_COOLDOWN_SECONDS)
        # 打开页面时由前端据此自动生成：还没有收藏模式结果，或结果已经过期。
        auto_start = bool(
            can_start and not recently_failed
            and (not published_library or stale))
        return {
            "mode": "library",
            "auto_start": auto_start,
            "library": {"members": len(library_ids)},
            "status": (
                latest_wire["status"] if latest_wire else "not_started"),
            "run": latest_wire,
            "results": results,
            "published_run_id": published.id if published else None,
            "batch_number": batch_number,
            "results_hidden_in_library": hidden_in_library,
            "history": history,
            "results_stale": stale,
            "can_start": can_start,
            "visual_model": visual_status,
            "dataset": {
                "positive_models": dataset.positive_count if dataset else 0,
                "negative_models": dataset.negative_count if dataset else 0,
                "labeled_models": len(dataset.labels) if dataset else 0,
                "decisive_pairs": dataset.decisive_pair_count if dataset else 0,
                "comparison_hash": current_hash,
                "error": dataset_error,
                "result_feedback": {
                    "positive_models": feedback_dataset.positive_count,
                    "negative_models": feedback_dataset.negative_count,
                    "labeled_models": len(feedback_dataset.labels),
                    "signature": feedback_dataset.signature,
                },
            },
            "safety": {
                "creates_tasks": False,
                "downloads_video": False,
                "uploads_to_115": False,
                "image_cache_only": True,
            },
        }

    async def record_feedback(
        self,
        model_id: int,
        action: str,
        *,
        session_factory=None,
    ) -> dict:
        if action not in FEEDBACK_ACTIONS:
            raise ValueError("未知的结果反馈")
        factory = session_factory or async_session
        async with factory() as session:
            published = (await session.execute(select(TasteModelRun).where(
                TasteModelRun.published.is_(True),
                TasteModelRun.status == "ready",
            ).order_by(TasteModelRun.id.desc()).limit(1))).scalar_one_or_none()
            if published is None:
                raise TasteModelUnavailable("当前没有已发布的首批结果")
            score = await session.get(TasteModelScore, (published.id, model_id))
            if score is None or int(score.rank) > RESULT_POOL:
                raise TasteModelUnavailable("该模特不在当前展示的首批结果中")
            await session.execute(update(TasteResultFeedback).where(
                TasteResultFeedback.model_id == model_id,
                TasteResultFeedback.active.is_(True),
            ).values(active=False))
            session.add(TasteResultFeedback(
                model_id=model_id,
                run_id=published.id,
                action=action,
                active=True,
            ))
            await session.commit()
        return {"model_id": model_id, "feedback": action}

    async def record_feedback_many(
        self,
        model_ids: list[int],
        action: str,
        *,
        session_factory=None,
    ) -> dict:
        """一次记录多位的同一评价（如“其余都不符合”）；任一不在当前批次则整批拒绝。"""
        if action not in FEEDBACK_ACTIONS:
            raise ValueError("未知的结果反馈")
        ids = sorted({int(model_id) for model_id in model_ids if int(model_id) > 0})
        if not ids:
            raise ValueError("没有要评价的模特")
        factory = session_factory or async_session
        async with factory() as session:
            published = (await session.execute(select(TasteModelRun).where(
                TasteModelRun.published.is_(True),
                TasteModelRun.status == "ready",
            ).order_by(TasteModelRun.id.desc()).limit(1))).scalar_one_or_none()
            if published is None:
                raise TasteModelUnavailable("当前没有已发布的首批结果")
            in_batch = set((await session.execute(select(TasteModelScore.model_id).where(
                TasteModelScore.run_id == published.id,
                TasteModelScore.model_id.in_(ids),
                TasteModelScore.rank <= RESULT_POOL,
            ))).scalars().all())
            if set(ids) - {int(model_id) for model_id in in_batch}:
                raise TasteModelUnavailable("部分模特不在当前展示的结果中，请刷新后重试")
            await session.execute(update(TasteResultFeedback).where(
                TasteResultFeedback.model_id.in_(ids),
                TasteResultFeedback.active.is_(True),
            ).values(active=False))
            session.add_all([TasteResultFeedback(
                model_id=model_id, run_id=published.id, action=action, active=True,
            ) for model_id in ids])
            await session.commit()
        return {"model_ids": ids, "feedback": action}

    async def liked_feedback(self, *, session_factory=None) -> list[dict]:
        """返回当前有效的“符合”记录，供人工确认后的媒体工作区使用。

        这里只读取偏好身份与来源批次，不创建同步、下载或上传任务。虽然写入路径
        会先停用同一模特的旧反馈，这里仍按 model_id 去重，避免旧库或并发异常让
        同一模特在工作区重复出现。
        """
        factory = session_factory or async_session
        async with factory() as session:
            rows = (await session.execute(select(TasteResultFeedback).where(
                TasteResultFeedback.active.is_(True),
                TasteResultFeedback.action == "like",
            ).order_by(
                TasteResultFeedback.created_at.desc(),
                TasteResultFeedback.id.desc(),
            ))).scalars().all()

        result: list[dict] = []
        seen: set[int] = set()
        for row in rows:
            model_id = int(row.model_id)
            if model_id in seen:
                continue
            seen.add(model_id)
            result.append({
                "feedback_id": int(row.id),
                "model_id": model_id,
                "run_id": int(row.run_id) if row.run_id is not None else None,
                "liked_at": row.created_at.isoformat() if row.created_at else None,
            })
        return result


taste_model_service = TasteModelService()
