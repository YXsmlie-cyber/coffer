"""审美校准：从非 115 模特中稳定抽取两两比较，并只记录本地选择。"""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from dataclasses import dataclass
from hashlib import sha256
import threading
import time

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError

from . import avatar_store
from .database import Candidate, Model, ModelPreviewVideo, TasteComparison, async_session
from .library_index import library_index
from .preview_service import previews_by_model


TASTE_GOAL = 20
TASTE_CHOICES = frozenset({"left", "right", "both", "neither", "skip"})


class TasteUnavailable(Exception):
    """当前候选池无法组成一对。"""


class TasteConflict(Exception):
    """客户端提交的候选对已经过期或已经被记录。"""


@dataclass(frozen=True)
class _ComparisonSummary:
    seen_model_ids: frozenset[int]
    breakdown: Counter
    reviewed_count: int
    latest_id: int | None


@dataclass(frozen=True)
class _EligibleModel:
    """候选页只需要的轻量字段，避免反复构造完整 ORM identity map。"""

    id: int
    ph_id: str
    profile_type: str
    profile_slug: str
    name: str
    url: str
    avatar_sha256: str
    rank: int
    cloud_cid: str


_CANDIDATE_CACHE_TTL = 5.0
_AVATAR_PATH_CACHE_TTL = 30.0
_candidate_cache: OrderedDict[
    object, tuple[float, tuple[_EligibleModel, ...]]
] = OrderedDict()
_avatar_path_cache: OrderedDict[str, tuple[float, bool]] = OrderedDict()
_avatar_cache_lock = threading.Lock()


async def _comparison_summary(session) -> _ComparisonSummary:
    rows = (await session.execute(select(
        TasteComparison.id,
        TasteComparison.left_model_id,
        TasteComparison.right_model_id,
        TasteComparison.choice,
    ).order_by(TasteComparison.id.asc()))).all()
    seen: set[int] = set()
    breakdown: Counter = Counter()
    for row in rows:
        seen.add(int(row.left_model_id))
        seen.add(int(row.right_model_id))
        breakdown[str(row.choice)] += 1
    return _ComparisonSummary(
        seen_model_ids=frozenset(seen),
        breakdown=breakdown,
        reviewed_count=len(rows),
        latest_id=int(rows[-1].id) if rows else None,
    )


async def _candidate_catalog(session) -> tuple[_EligibleModel, ...]:
    """短时缓存稳定候选元数据；比较进度和 115 成员仍在每次请求过滤。"""
    cache_key = session.bind
    now = time.monotonic()
    cached = _candidate_cache.get(cache_key)
    if cached is not None and cached[0] > now:
        _candidate_cache.move_to_end(cache_key)
        return cached[1]
    query = select(
        Model.id,
        Model.ph_id,
        Model.profile_type,
        Model.profile_slug,
        Model.name,
        Model.url,
        Model.avatar_sha256,
        Model.rank,
        Model.cloud_cid,
    ).where(
        Model.profile_type.in_(("model", "pornstar")),
        Model.avatar_status == "ready",
        Model.avatar_sha256 != "",
        exists().where(ModelPreviewVideo.model_id == Model.id),
        ~exists().where(
            Candidate.model_id == Model.id,
            Candidate.gender_status == "excluded_non_performer",
        ),
    ).order_by(Model.id.asc())
    rows = (await session.execute(query)).all()
    result = tuple(_EligibleModel(
        id=int(row.id),
        ph_id=str(row.ph_id or ""),
        profile_type=str(row.profile_type or "model"),
        profile_slug=str(row.profile_slug or ""),
        name=str(row.name or ""),
        url=str(row.url or ""),
        avatar_sha256=str(row.avatar_sha256 or ""),
        rank=int(row.rank or 0),
        cloud_cid=str(row.cloud_cid or ""),
    ) for row in rows)
    _candidate_cache[cache_key] = (now + _CANDIDATE_CACHE_TTL, result)
    _candidate_cache.move_to_end(cache_key)
    while len(_candidate_cache) > 8:
        _candidate_cache.popitem(last=False)
    return result


def _has_local_avatar(model: _EligibleModel) -> bool:
    digest = model.avatar_sha256.strip().casefold()
    with _avatar_cache_lock:
        now = time.monotonic()
        cached = _avatar_path_cache.get(digest)
        if cached is not None and cached[0] > now:
            _avatar_path_cache.move_to_end(digest)
            return cached[1]
        available = avatar_store.avatar_path(digest) is not None
        _avatar_path_cache[digest] = (now + _AVATAR_PATH_CACHE_TTL, available)
        _avatar_path_cache.move_to_end(digest)
        while len(_avatar_path_cache) > 4096:
            _avatar_path_cache.popitem(last=False)
        return available


async def _eligible_models(
    session, seen_model_ids: frozenset[int],
) -> list[_EligibleModel]:
    models = await _candidate_catalog(session)
    membership = await library_index.membership(session=session)
    candidates = [
        model for model in models
        if model.id not in seen_model_ids
        and not membership.contains(model)
    ]
    # 首次请求可能需要检查数千个 sha 文件；缓存未命中时统一放在线程中，
    # 后续 30 秒直接走内存，不阻塞 FastAPI 事件循环。
    return await asyncio.to_thread(
        lambda: [model for model in candidates if _has_local_avatar(model)])


def _stable_pair(
    models: list[_EligibleModel], reviewed_count: int,
) -> tuple[_EligibleModel, _EligibleModel] | None:
    if len(models) < 2:
        return None
    # 同一比较进度下顺序完全稳定，刷新页面不会无记录地换一组；记录后盐值改变。
    salt = f"ph115-taste-v1:{reviewed_count}:{len(models)}"
    ordered = sorted(
        models,
        key=lambda model: sha256(f"{salt}:{model.id}".encode("ascii")).digest(),
    )
    return ordered[0], ordered[1]


def _model_wire(model: _EligibleModel, previews: list[dict]) -> dict:
    public_slug = str(model.profile_slug or model.ph_id).split(":", 1)[-1]
    digest = str(model.avatar_sha256 or "").strip().casefold()
    return {
        "id": model.id,
        "ph_id": public_slug,
        "profile_type": model.profile_type or "model",
        "profile_slug": public_slug,
        "name": model.name,
        "url": model.url,
        "avatar_url": f"/api/models/{model.id}/avatar?v={digest[:16]}",
        "rank": int(model.rank or 0),
        "preview_videos": previews[:4],
    }


def _stats(summary: _ComparisonSummary, available_candidates: int) -> dict:
    breakdown = {
        choice: int(summary.breakdown.get(choice, 0))
        for choice in ("left", "right", "both", "neither", "skip")
    }
    answered_count = (
        breakdown["left"] + breakdown["right"]
        + breakdown["both"] + breakdown["neither"]
    )
    return {
        "reviewed_count": summary.reviewed_count,
        "answered_count": answered_count,
        "skipped_count": breakdown["skip"],
        "goal": TASTE_GOAL,
        "goal_remaining": max(0, TASTE_GOAL - answered_count),
        "progress_percent": min(100, round(answered_count / TASTE_GOAL * 100)),
        "available_candidates": available_candidates,
        "has_undo": summary.latest_id is not None,
        "breakdown": breakdown,
    }


async def _state(session) -> dict:
    summary = await _comparison_summary(session)
    models = await _eligible_models(session, summary.seen_model_ids)
    selected = _stable_pair(models, summary.reviewed_count)
    pair = None
    if selected is not None:
        left, right = selected
        previews = await previews_by_model(session, [left.id, right.id])
        pair = {
            "left": _model_wire(left, previews.get(left.id, [])),
            "right": _model_wire(right, previews.get(right.id, [])),
        }
    return {
        "pair": pair,
        "stats": _stats(summary, len(models)),
        "library_filter": "exclude",
    }


async def get_state(*, session_factory=None) -> dict:
    factory = session_factory or async_session
    async with factory() as session:
        return await _state(session)


async def record_comparison(
    left_model_id: int,
    right_model_id: int,
    choice: str,
    *,
    lock,
    session_factory=None,
) -> dict:
    if choice not in TASTE_CHOICES:
        raise ValueError("未知的审美校准选择")
    factory = session_factory or async_session
    async with lock:
        async with factory() as session:
            summary = await _comparison_summary(session)
            models = await _eligible_models(session, summary.seen_model_ids)
            selected = _stable_pair(models, summary.reviewed_count)
            if selected is None:
                raise TasteUnavailable("当前没有足够的可比较候选")
            left, right = selected
            if (left.id, right.id) != (left_model_id, right_model_id):
                raise TasteConflict("这组候选已经变化，请刷新后重新选择")

            comparison = TasteComparison(
                pair_key=f"{left.id}:{right.id}",
                left_model_id=left.id,
                right_model_id=right.id,
                choice=choice,
            )
            session.add(comparison)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise TasteConflict("这组候选已经记录，请刷新查看下一组") from exc
            return await _state(session)


async def undo_latest(*, lock, session_factory=None) -> dict:
    factory = session_factory or async_session
    async with lock:
        async with factory() as session:
            latest = (await session.execute(select(TasteComparison).order_by(
                TasteComparison.id.desc()
            ).limit(1))).scalar_one_or_none()
            if latest is None:
                raise TasteUnavailable("还没有可撤销的校准记录")
            await session.delete(latest)
            await session.commit()
            return await _state(session)
