"""“探索”页的本地视觉特征层。

这里只负责验证 ONNX 资产、缓存头像/首张封面的独立向量，以及按固定口径聚合
模型级特征。它不包含推荐来源、任务调度、115 写入或视频下载逻辑。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import time
from typing import Awaitable, Callable

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import avatar_store, preview_image_store
from .config import settings
from .database import FeatureVector, Model, ModelPreviewVideo, async_session


FEATURE_AVATAR = "taste_visual_avatar"
FEATURE_COVER = "taste_visual_first_cover"
FEATURE_MODE = "avatar_first_cover_50_50"
MANIFEST_NAME = "siglip2-int8.json"
ProgressCallback = Callable[[str, int, int], Awaitable[None]]
# 封面补齐的时间边界：单张最多等 20 秒，超时的封面冷却 6 小时不再重试；
# 已有封面足够时，整段补齐最多 45 秒，剩余的留给之后的批次逐步补齐。
COVER_TIMEOUT_CAP_SECONDS = 20.0
COVER_FETCH_BUDGET_SECONDS = 45.0
COVER_TIMEOUT_FAILURE_TTL = 6 * 60 * 60.0


class VisualModelUnavailable(RuntimeError):
    """本地视觉资产缺失或未通过完整性/质量门禁。"""


@dataclass(frozen=True)
class VisualAsset:
    model_id: int
    feature_type: str
    item_key: str
    path: Path
    input_hash: str


@dataclass(frozen=True)
class FeatureIndexResult:
    requested_models: int
    ready_models: int
    excluded_models: int
    inferred_images: int
    cached_images: int
    cover_fetch_total: int
    cover_fetch_succeeded: int
    model_version: str
    excluded_model_ids: tuple[int, ...]
    cover_fetch_deferred: int = 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _cached_cover_paths(rows: list[tuple[int, str]]) -> list[tuple[int, Path | None]]:
    """在线程中检查一小批封面；无效键与缺文件沿用相同的缺项语义。"""
    paths = []
    for model_id, viewkey in rows:
        try:
            path = preview_image_store.preview_path(viewkey)
        except preview_image_store.InvalidPreviewViewkey:
            path = None
        paths.append((model_id, path))
    return paths


def _local_visual_assets(
    rows: list[tuple[int, str, str | None, Path | None]],
) -> tuple[list[VisualAsset], set[int]]:
    """在线程中顺序检查头像并完整哈希封面，不缓存跨轮次的文件判断。"""
    assets = []
    ready_models = set()
    for model_id, avatar_digest, viewkey, cover_path in rows:
        avatar_path = avatar_store.avatar_path(avatar_digest)
        if avatar_path is None or viewkey is None or cover_path is None:
            continue
        avatar_hash = str(avatar_digest or "").strip().casefold()
        cover_hash = _sha256(cover_path)
        assets.extend((
            VisualAsset(
                model_id=model_id, feature_type=FEATURE_AVATAR, item_key="avatar",
                path=avatar_path, input_hash=avatar_hash,
            ),
            VisualAsset(
                model_id=model_id, feature_type=FEATURE_COVER, item_key="first_cover",
                path=cover_path, input_hash=hashlib.sha256(
                    f"{viewkey}:{cover_hash}".encode("utf-8")
                ).hexdigest(),
            ),
        ))
        ready_models.add(model_id)
    return assets, ready_models


@lru_cache(maxsize=1)
def load_visual_manifest() -> dict:
    """读取并严格验证当前唯一允许使用的视觉模型清单。"""
    if not settings.taste_visual_enabled:
        raise VisualModelUnavailable("本地视觉验证已通过配置关闭")
    root = Path(settings.taste_visual_model_dir).expanduser().resolve()
    manifest_path = root / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VisualModelUnavailable(f"视觉模型清单不存在：{manifest_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise VisualModelUnavailable(f"视觉模型清单无法读取：{exc}") from exc

    try:
        model_path = (root / str(manifest["file"])).resolve()
        expected_hash = str(manifest["sha256"]).strip().casefold()
        version = str(manifest["version"]).strip()
        quality_passed = bool(manifest["quality_passed"])
        batch_dynamic = bool(manifest["dynamic_batch"])
        image_size = int(manifest["image_size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VisualModelUnavailable("视觉模型清单字段不完整") from exc
    if model_path.parent != root:
        raise VisualModelUnavailable("视觉模型文件必须位于配置目录内")
    if not quality_passed:
        raise VisualModelUnavailable("视觉模型清单未通过量化质量门禁")
    if not batch_dynamic or image_size != 224:
        raise VisualModelUnavailable("视觉模型结构与 224×224 动态批次约定不一致")
    if not version or len(expected_hash) != 64 or not model_path.is_file():
        raise VisualModelUnavailable("视觉模型版本、哈希或文件缺失")
    actual_hash = _sha256(model_path)
    if actual_hash != expected_hash:
        raise VisualModelUnavailable(
            f"视觉模型 SHA-256 不一致：期望 {expected_hash}，实际 {actual_hash}")
    validated = dict(manifest)
    validated["path"] = str(model_path)
    validated["manifest_path"] = str(manifest_path)
    return validated


_WORKER_SESSION = None
_WORKER_SESSION_KEY = None


def _encode_batch(payload: dict) -> np.ndarray:
    """在独立进程中执行一批图片推理，避免 ONNX Runtime 占用 API 主事件循环。"""
    import onnxruntime as ort
    from PIL import Image

    manifest = payload["manifest"]
    global _WORKER_SESSION, _WORKER_SESSION_KEY
    session_key = (manifest["path"], manifest["sha256"], int(payload["threads"]))
    if _WORKER_SESSION is None or _WORKER_SESSION_KEY != session_key:
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(8, max(1, int(payload["threads"])))
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        _WORKER_SESSION = ort.InferenceSession(
            manifest["path"],
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        _WORKER_SESSION_KEY = session_key

    session = _WORKER_SESSION
    input_name = session.get_inputs()[0].name
    layout = str(manifest.get("layout", "NCHW")).upper()
    image_size = int(manifest.get("image_size", 224))
    mean = np.asarray(manifest.get("mean", [0.5, 0.5, 0.5]), dtype=np.float32)
    std = np.asarray(manifest.get("std", [0.5, 0.5, 0.5]), dtype=np.float32)
    tensors: list[np.ndarray] = []
    for raw_path in payload["paths"]:
        with Image.open(raw_path) as image:
            image = image.convert("RGB").resize(
                (image_size, image_size), Image.Resampling.BICUBIC)
            array = np.asarray(image, dtype=np.float32) / 255.0
        array = (array - mean) / std
        if layout == "NCHW":
            array = np.transpose(array, (2, 0, 1))
        tensors.append(array)
    batch = np.stack(tensors).astype(np.float32, copy=False)
    outputs = session.run(None, {input_name: batch})
    encoded = np.asarray(
        outputs[int(manifest.get("output_index", 0))], dtype=np.float32)
    if encoded.ndim == 3:
        encoded = encoded.mean(axis=1)
    elif encoded.ndim > 2:
        encoded = encoded.reshape((encoded.shape[0], -1))
    norm = np.linalg.norm(encoded, axis=1, keepdims=True)
    encoded = encoded / np.maximum(norm, 1e-12)
    # ndarray 通过 multiprocessing 的二进制 pickle 传输，避免数百万 Python
    # float/list 对象的构造、GC 与 IPC 膨胀。
    return np.ascontiguousarray(encoded, dtype=np.float32)


def _normalize(vector: np.ndarray) -> np.ndarray:
    value = np.asarray(vector, dtype=np.float32)
    return value / max(float(np.linalg.norm(value)), 1e-12)


class TasteVisualFeatures:
    def __init__(self):
        self._executor: ProcessPoolExecutor | None = None
        self._index_lock = asyncio.Lock()

    def status(self) -> dict:
        try:
            manifest = load_visual_manifest()
        except VisualModelUnavailable as exc:
            return {"available": False, "error": str(exc), "model_version": None}
        return {
            "available": True,
            "error": "",
            "model_version": manifest["version"],
            "model_name": manifest.get("model", ""),
        }

    async def close(self):
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    async def _models_and_first_previews(
        self, model_ids: list[int], *, session_factory,
    ) -> tuple[dict[int, Model], dict[int, ModelPreviewVideo]]:
        ids = sorted({int(value) for value in model_ids if int(value) > 0})
        if not ids:
            return {}, {}
        async with session_factory() as session:
            models = (await session.execute(select(Model).where(
                Model.id.in_(ids)
            ))).scalars().all()
            previews = (await session.execute(select(ModelPreviewVideo).where(
                ModelPreviewVideo.model_id.in_(ids),
                ModelPreviewVideo.ordinal == 0,
            ))).scalars().all()
        first = {int(preview.model_id): preview for preview in previews}
        return {int(model.id): model for model in models}, first

    async def _prepare_assets(
        self,
        model_ids: list[int],
        *,
        fetch_missing_covers: bool,
        progress: ProgressCallback | None,
        session_factory,
        min_cover_ratio: float | None = None,
    ) -> tuple[list[VisualAsset], set[int], int, int, int]:
        models, first_previews = await self._models_and_first_previews(
            model_ids, session_factory=session_factory)
        cover_paths: dict[int, Path] = {}
        missing_cover_rows: list[tuple[int, ModelPreviewVideo]] = []
        preview_rows = [(model_id, preview.viewkey)
                        for model_id, preview in first_previews.items()]
        for start in range(0, len(preview_rows), 128):
            paths = await asyncio.to_thread(
                _cached_cover_paths, preview_rows[start:start + 128])
            for model_id, path in paths:
                if path is not None:
                    cover_paths[model_id] = path
                elif fetch_missing_covers:
                    missing_cover_rows.append((model_id, first_previews[model_id]))

        fetch_succeeded = 0
        deferred = 0
        if missing_cover_rows:
            completed = 0
            queue: asyncio.Queue[tuple[int, ModelPreviewVideo]] = asyncio.Queue()
            for row in missing_cover_rows:
                queue.put_nowait(row)
            per_cover_timeout = min(
                float(settings.taste_visual_cover_timeout), COVER_TIMEOUT_CAP_SECONDS)
            deadline = time.monotonic() + COVER_FETCH_BUDGET_SECONDS
            total_previews = max(1, len(first_previews))

            def may_defer_rest() -> bool:
                # 只有已有封面足以越过覆盖率门槛时才提前收手；首次全量补齐不受限。
                return (min_cover_ratio is not None
                        and time.monotonic() >= deadline
                        and len(cover_paths) >= min_cover_ratio * total_previews)

            async def worker():
                nonlocal completed, fetch_succeeded
                while True:
                    if may_defer_rest():
                        return
                    try:
                        model_id, preview = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        try:
                            path = await asyncio.wait_for(
                                preview_image_store.ensure_preview_image(preview.viewkey),
                                timeout=per_cover_timeout,
                            )
                        except TimeoutError:
                            # 超时不是封面服务自己的失败，不会进入其冷却缓存；
                            # 这里登记后，下一批不会再为同一张封面白等。
                            preview_image_store.remember_failure(
                                preview.viewkey, "封面抓取超时", COVER_TIMEOUT_FAILURE_TTL)
                            path = None
                        except (
                            preview_image_store.PreviewImageError,
                            OSError,
                            ValueError,
                        ):
                            path = None
                        completed += 1
                        if path is not None:
                            cover_paths[model_id] = path
                            fetch_succeeded += 1
                        if progress is not None:
                            await progress(
                                "fetching_covers", completed, len(missing_cover_rows))
                    finally:
                        queue.task_done()

            worker_count = min(
                len(missing_cover_rows),
                max(1, int(settings.preview_image_fetch_concurrency)),
            )
            tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
            try:
                await asyncio.gather(*tasks)
            finally:
                # progress 回调或未知文件错误若意外抛出，不能留下无引用 worker
                # 继续下载封面并占用全局网络槽。
                pending = [task for task in tasks if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            deferred = queue.qsize()
            if deferred and progress is not None:
                # 剩余封面留到之后的批次；进度条直接走完，不让界面停在半截。
                await progress(
                    "fetching_covers", len(missing_cover_rows), len(missing_cover_rows))

        assets: list[VisualAsset] = []
        ready_models: set[int] = set()
        local_rows = []
        for model_id in sorted(models):
            model = models[model_id]
            preview = first_previews.get(model_id)
            local_rows.append((
                model_id, model.avatar_sha256,
                preview.viewkey if preview is not None else None,
                cover_paths.get(model_id),
            ))
        # 每批只有原始标量/路径传入线程，保留模型顺序和完整文件哈希，
        # 避免主循环 stat，也避免数千张封面逐张调度线程的开销。
        for start in range(0, len(local_rows), 128):
            batch_assets, batch_ready = await asyncio.to_thread(
                _local_visual_assets, local_rows[start:start + 128])
            assets.extend(batch_assets)
            ready_models.update(batch_ready)
        return assets, ready_models, len(missing_cover_rows), fetch_succeeded, deferred

    async def _existing_keys(
        self, assets: list[VisualAsset], model_version: str, *, session_factory,
    ) -> set[tuple[int, str, str]]:
        if not assets:
            return set()
        model_ids = sorted({asset.model_id for asset in assets})
        async with session_factory() as session:
            rows = (await session.execute(select(
                FeatureVector.model_id,
                FeatureVector.feature_type,
                FeatureVector.item_key,
                FeatureVector.input_hash,
                FeatureVector.dimensions,
                func.length(FeatureVector.vector).label("vector_bytes"),
            ).where(
                FeatureVector.model_id.in_(model_ids),
                FeatureVector.feature_type.in_((FEATURE_AVATAR, FEATURE_COVER)),
                FeatureVector.model_version == model_version,
                FeatureVector.status == "ready",
            ))).all()
        wanted = {
            (asset.model_id, asset.feature_type, asset.item_key): asset.input_hash
            for asset in assets
        }
        return {
            (row.model_id, row.feature_type, row.item_key)
            for row in rows
            if int(row.vector_bytes or 0) > 0
            and row.dimensions > 0
            and wanted.get((row.model_id, row.feature_type, row.item_key)) == row.input_hash
            and int(row.vector_bytes or 0) == row.dimensions * 4
        }

    async def _upsert_vectors(
        self,
        rows: list[tuple[VisualAsset, np.ndarray]],
        model_version: str,
        *,
        session_factory,
    ):
        if not rows:
            return
        now = datetime.now()
        values = [{
            "model_id": asset.model_id,
            "feature_type": asset.feature_type,
            "item_key": asset.item_key,
            "model_version": model_version,
            "input_hash": asset.input_hash,
            "dimensions": int(vector.shape[0]),
            "vector": vector.astype("<f4", copy=False).tobytes(),
            "status": "ready",
            "error": "",
            "updated_at": now,
        } for asset, vector in rows]
        statement = sqlite_insert(FeatureVector).values(values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                "model_id", "feature_type", "item_key", "model_version",
            ],
            set_={
                "input_hash": statement.excluded.input_hash,
                "dimensions": statement.excluded.dimensions,
                "vector": statement.excluded.vector,
                "status": statement.excluded.status,
                "error": statement.excluded.error,
                "updated_at": statement.excluded.updated_at,
            },
        )
        async with session_factory() as session:
            await session.execute(statement)
            await session.commit()

    async def _infer_assets(
        self,
        assets: list[VisualAsset],
        manifest: dict,
        *,
        progress: ProgressCallback | None,
        session_factory,
    ) -> int:
        if not assets:
            return 0
        if self._executor is None:
            self._executor = ProcessPoolExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        processed = 0
        inferred_count = 0
        batch_size = int(settings.taste_visual_batch_size)
        write_buffer: list[tuple[VisualAsset, np.ndarray]] = []
        for start in range(0, len(assets), batch_size):
            batch = assets[start:start + batch_size]
            attempted = len(batch)
            payload = {
                "manifest": manifest,
                "threads": int(settings.taste_visual_max_threads),
                "paths": [str(asset.path) for asset in batch],
            }
            encoded: np.ndarray | list[np.ndarray]
            try:
                encoded = await loop.run_in_executor(
                    self._executor, _encode_batch, payload)
            except Exception:
                # 单张损坏不应让整个候选池失败；批次异常时逐张隔离。
                encoded = []
                valid_assets: list[VisualAsset] = []
                for asset in batch:
                    try:
                        one = await loop.run_in_executor(self._executor, _encode_batch, {
                            **payload, "paths": [str(asset.path)],
                        })
                    except Exception:
                        continue
                    encoded.extend(one)
                    valid_assets.append(asset)
                batch = valid_assets
            rows: list[tuple[VisualAsset, np.ndarray]] = []
            for asset, raw_vector in zip(batch, encoded):
                vector = _normalize(np.asarray(raw_vector, dtype=np.float32))
                if vector.ndim == 1 and vector.size:
                    rows.append((asset, vector))
            write_buffer.extend(rows)
            inferred_count += len(rows)
            # 推理仍保持 8～16 张一批；SQLite 则合并成约 60 张一笔短事务，
            # 避免全量生成时和任务状态写入争抢数百次写锁。
            if len(write_buffer) >= 60 or start + batch_size >= len(assets):
                await self._upsert_vectors(
                    write_buffer,
                    str(manifest["version"]),
                    session_factory=session_factory,
                )
                write_buffer = []
            processed += attempted
            if progress is not None:
                await progress("indexing", processed, len(assets))
        return inferred_count

    async def index_models(
        self,
        model_ids: list[int],
        *,
        fetch_missing_covers: bool,
        progress: ProgressCallback | None = None,
        session_factory=None,
        min_cover_ratio: float | None = None,
    ) -> FeatureIndexResult:
        """为每位模特准备头像 + 第一封面，并增量写入两个独立向量。"""
        factory = session_factory or async_session
        ids = sorted({int(value) for value in model_ids if int(value) > 0})
        # 首次校验会顺序读取并哈希约 100 MiB ONNX 文件，不能占用 API 事件循环。
        manifest = await asyncio.to_thread(load_visual_manifest)
        async with self._index_lock:
            assets, prepared_models, fetch_total, fetch_succeeded, fetch_deferred = (
                await self._prepare_assets(
                    ids,
                    fetch_missing_covers=fetch_missing_covers,
                    progress=progress,
                    session_factory=factory,
                    min_cover_ratio=min_cover_ratio,
                )
            )
            existing = await self._existing_keys(
                assets, str(manifest["version"]), session_factory=factory)
            pending = [asset for asset in assets if (
                asset.model_id, asset.feature_type, asset.item_key) not in existing]
            inferred = await self._infer_assets(
                pending,
                manifest,
                progress=progress,
                session_factory=factory,
            )
            ready_keys = await self._existing_keys(
                assets, str(manifest["version"]), session_factory=factory)
            ready_parts: dict[int, set[str]] = {}
            for model_id, feature_type, _item_key in ready_keys:
                ready_parts.setdefault(int(model_id), set()).add(feature_type)
            ready = {
                model_id for model_id, feature_types in ready_parts.items()
                if {FEATURE_AVATAR, FEATURE_COVER}.issubset(feature_types)
            }
            excluded = tuple(sorted(set(ids) - ready))
            return FeatureIndexResult(
                requested_models=len(ids),
                ready_models=len(ready),
                excluded_models=len(excluded),
                inferred_images=inferred,
                cached_images=max(0, len(assets) - len(pending)),
                cover_fetch_total=fetch_total,
                cover_fetch_succeeded=fetch_succeeded,
                model_version=str(manifest["version"]),
                excluded_model_ids=excluded,
                cover_fetch_deferred=fetch_deferred,
            )

    async def load_feature_matrices(
        self, model_ids: list[int], *, session_factory=None,
    ) -> dict[str, dict[int, np.ndarray]]:
        """返回统一归一化的头像诊断特征与 50/50 正式融合特征。"""
        factory = session_factory or async_session
        ids = sorted({int(value) for value in model_ids if int(value) > 0})
        if not ids:
            return {"avatar_only": {}, FEATURE_MODE: {}}
        manifest = await asyncio.to_thread(load_visual_manifest)
        async with factory() as session:
            rows = (await session.execute(select(FeatureVector).where(
                FeatureVector.model_id.in_(ids),
                FeatureVector.feature_type.in_((FEATURE_AVATAR, FEATURE_COVER)),
                FeatureVector.model_version == str(manifest["version"]),
                FeatureVector.status == "ready",
            ))).scalars().all()
        avatars: dict[int, np.ndarray] = {}
        covers: dict[int, np.ndarray] = {}
        for row in rows:
            if not row.vector or row.dimensions <= 0 or len(row.vector) != row.dimensions * 4:
                continue
            vector = _normalize(np.frombuffer(row.vector, dtype="<f4").copy())
            if row.feature_type == FEATURE_AVATAR and row.item_key == "avatar":
                avatars[int(row.model_id)] = vector
            elif row.feature_type == FEATURE_COVER and row.item_key == "first_cover":
                covers[int(row.model_id)] = vector
        fused = {
            model_id: _normalize(0.5 * avatar + 0.5 * covers[model_id])
            for model_id, avatar in avatars.items()
            if model_id in covers
        }
        return {"avatar_only": avatars, FEATURE_MODE: fused}


taste_visual_features = TasteVisualFeatures()
