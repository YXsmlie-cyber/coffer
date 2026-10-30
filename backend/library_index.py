"""115 VIDEO_HUB 目录索引与统一库成员判断。

所有页面和推荐任务都通过本模块判断“是否已经在115”。刷新采用先读远端、
后短事务发布的方式；远端读取失败不会覆盖最后一次成功快照。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import time

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from .cloud115 import cloud
from .identity import normalize_library_name
from .database import (
    CloudModelFolder,
    LibraryScanRun,
    Model,
    ModelAlias,
    async_session,
)


@dataclass(frozen=True)
class LibraryMembership:
    model_ids: frozenset[int]
    normalized_names: frozenset[str]
    folders_by_model: dict[int, dict]
    scan_generation: int | None
    last_success_at: str | None
    stale: bool

    def contains(self, model: Model) -> bool:
        return (
            model.id in self.model_ids
            or bool(str(model.cloud_cid or "").strip())
            or normalize_library_name(model.name) in self.normalized_names
        )


class LibraryIndex:
    def __init__(self):
        self._refresh_lock = asyncio.Lock()
        self._membership_lock = asyncio.Lock()
        self._membership_cache: dict[object, tuple[float, LibraryMembership]] = {}
        self._membership_generation = 0
        self._broadcast = None

    def _invalidate_membership(self):
        self._membership_generation += 1
        self._membership_cache.clear()

    def set_broadcast(self, callback):
        self._broadcast = callback

    async def _emit(self, message: dict):
        if self._broadcast:
            await self._broadcast(message)

    async def ensure_legacy_index(self):
        """从既有 Model.cloud_cid 回填；不访问115，启动可离线完成。"""
        now = datetime.now()
        async with async_session() as session:
            models = (await session.execute(select(Model).where(
                Model.cloud_cid != ""
            ))).scalars().all()
            changed = False
            existing = {
                folder.cid: folder for folder in (await session.execute(
                    select(CloudModelFolder).where(CloudModelFolder.cid.in_([
                        str(model.cloud_cid or "").strip() for model in models
                        if str(model.cloud_cid or "").strip()
                    ]))
                )).scalars().all()
            } if models else {}
            for model in models:
                cid = str(model.cloud_cid or "").strip()
                if not cid:
                    continue
                folder = existing.get(cid)
                if folder is None:
                    session.add(CloudModelFolder(
                        cid=cid,
                        name=model.name,
                        normalized_name=normalize_library_name(model.name),
                        model_id=model.id,
                        match_method="legacy_cid",
                        active=True,
                        last_seen_at=now,
                    ))
                    changed = True
                elif folder.model_id is None:
                    folder.model_id = model.id
                    folder.match_method = "legacy_cid"
                    folder.active = True
                    changed = True
            if changed:
                try:
                    await session.commit()
                except IntegrityError:
                    # 旧库若存在同一模特多 CID，保留 Model.cloud_cid，等待刷新页人工处理。
                    await session.rollback()
            if changed:
                self._invalidate_membership()

    async def refresh(self) -> dict:
        """读取完整根目录后原子发布新索引；失败只记录失败代次。"""
        async with self._refresh_lock:
            started = datetime.now()
            async with async_session() as session:
                run = LibraryScanRun(status="running", started_at=started)
                session.add(run)
                await session.commit()
                await session.refresh(run)
                run_id = run.id

            await self._emit({
                "type": "library_refresh_progress",
                "status": "running",
                "scan_generation": run_id,
            })
            try:
                remote_dirs = await cloud.list_model_dirs()
                result = await self._publish_scan(run_id, remote_dirs)
            except Exception as error:
                async with async_session() as session:
                    failed = await session.get(LibraryScanRun, run_id)
                    if failed:
                        failed.status = "failed"
                        failed.error = str(error)[:2000]
                        failed.completed_at = datetime.now()
                        await session.commit()
                await self._emit({
                    "type": "library_refresh_progress",
                    "status": "failed",
                    "scan_generation": run_id,
                    "error": str(error),
                })
                raise
            await self._emit({
                "type": "library_refresh_progress",
                "status": "completed",
                **result,
            })
            return result

    async def _publish_scan(self, run_id: int, remote_dirs: list[dict]) -> dict:
        now = datetime.now()
        cleaned: list[tuple[str, str, str]] = []
        seen_cids: set[str] = set()
        for item in remote_dirs:
            cid = str(item.get("cid") or "").strip()
            name = " ".join(str(item.get("name") or "").strip().split())
            if not cid or not name or cid in seen_cids:
                continue
            seen_cids.add(cid)
            cleaned.append((cid, name, normalize_library_name(name)))

        if not cleaned:
            # 115偶尔会在登录失效或接口降级时返回结构正常但内容为空的响应。
            # 已有有效快照时绝不能把这种“假成功”发布成空库，否则普通页面会瞬间
            # 泄漏全部115模特。首次初始化且本来没有目录时仍允许建立空快照。
            async with async_session() as session:
                active_count = int((await session.execute(select(func.count()).select_from(
                    CloudModelFolder
                ).where(CloudModelFolder.active.is_(True)))).scalar_one())
            if active_count:
                raise RuntimeError(
                    f"115返回空目录，已拒绝覆盖现有{active_count}个有效目录")

        async with async_session() as session:
            models = (await session.execute(select(Model))).scalars().all()
            aliases = (await session.execute(select(ModelAlias).where(
                ModelAlias.trusted.is_(True)
            ))).scalars().all()
            existing = (await session.execute(select(CloudModelFolder))).scalars().all()

            model_by_id = {model.id: model for model in models}
            by_cid = {
                str(model.cloud_cid).strip(): model
                for model in models if str(model.cloud_cid or "").strip()
            }
            by_name: dict[str, list[Model]] = {}
            for model in models:
                by_name.setdefault(normalize_library_name(model.name), []).append(model)
            alias_by_name: dict[str, Model] = {}
            for alias in aliases:
                model = model_by_id.get(alias.model_id)
                if model:
                    alias_by_name[alias.normalized_alias] = model
            folders_by_cid = {folder.cid: folder for folder in existing}

            previous_binding_by_cid = {
                folder.cid: (folder.model_id, folder.match_method)
                for folder in existing if folder.model_id
            }

            for folder in existing:
                folder.active = False
                # model_id 有唯一约束；发布新快照时先释放旧占用，再按完整快照重绑。
                folder.model_id = None

            # 这里必须单独 flush：若在同一次 flush 中同时把旧目录解绑、把新目录
            # 绑定到同一模特，SQLite 可能先执行后者并撞上 model_id 唯一约束。
            # 两阶段发布仍位于同一个事务内，后续任一步失败都会整体回滚。
            await session.flush()

            active_folders: list[tuple[CloudModelFolder, str, str]] = []
            for cid, name, normalized in cleaned:
                folder = folders_by_cid.get(cid)
                if folder is None:
                    folder = CloudModelFolder(
                        cid=cid,
                        name=name,
                        normalized_name=normalized,
                    )
                    session.add(folder)
                    folders_by_cid[cid] = folder

                folder.name = name
                folder.normalized_name = normalized
                folder.active = True
                folder.scan_run_id = run_id
                folder.last_seen_at = now
                folder.model_id = None
                folder.match_method = "unbound"
                folder.error = ""
                active_folders.append((folder, cid, normalized))

            claimed_models: set[int] = set()
            bound_count = 0
            error_count = 0

            def bind_folder(
                folder: CloudModelFolder,
                cid: str,
                candidate: Model,
                method: str,
            ) -> bool:
                nonlocal bound_count, error_count
                if candidate.id in claimed_models:
                    folder.error = "同一模特已绑定到另一个115目录，请人工处理"
                    error_count += 1
                    return False
                folder.model_id = candidate.id
                folder.match_method = method
                candidate.cloud_cid = cid
                claimed_models.add(candidate.id)
                bound_count += 1
                return True

            # 第一遍只恢复仍存在的历史绑定。人工绑定和既有精确绑定都比目录名
            # 推断更可信，不能被远端列表中更靠前的同名未绑定目录抢占。
            for folder, cid, _normalized in active_folders:
                previous = previous_binding_by_cid.get(cid)
                if not previous:
                    continue
                previous_model_id, previous_method = previous
                candidate = model_by_id.get(previous_model_id)
                if candidate:
                    bind_folder(
                        folder,
                        cid,
                        candidate,
                        previous_method or "manual",
                    )

            # 第二遍才使用旧 Model.cloud_cid、可信别名和唯一精确名称补齐未绑定目录。
            for folder, cid, normalized in active_folders:
                if folder.model_id is not None or folder.error:
                    continue
                candidate: Model | None = None
                method = "unbound"
                if cid in by_cid:
                    candidate = by_cid[cid]
                    method = "legacy_cid"
                elif normalized in alias_by_name:
                    candidate = alias_by_name[normalized]
                    method = "trusted_alias"
                elif len(by_name.get(normalized, ())) == 1:
                    candidate = by_name[normalized][0]
                    method = "exact_name"
                if candidate:
                    bind_folder(folder, cid, candidate, method)

            active_cids = {cid for cid, _, _ in cleaned}
            for model in models:
                current_cid = str(model.cloud_cid or "").strip()
                if current_cid and current_cid not in active_cids:
                    model.cloud_cid = ""

            run = await session.get(LibraryScanRun, run_id)
            if run is None:
                raise RuntimeError("115扫描代次在发布前丢失")
            run.status = "completed"
            run.folder_count = len(cleaned)
            run.bound_count = bound_count
            run.error = ""
            run.completed_at = now
            await session.commit()

        self._invalidate_membership()

        return {
            "scan_generation": run_id,
            "folder_count": len(cleaned),
            "bound_count": bound_count,
            "unbound_count": len(cleaned) - bound_count,
            "error_count": error_count,
            "last_success_at": now.isoformat(),
            "stale": False,
        }

    async def membership(self, *, session=None) -> LibraryMembership:
        owns_session = session is None
        if owns_session:
            session = async_session()
        try:
            # 直接持有 engine/bind 对象，避免短生命周期测试引擎销毁后 Python id
            # 被复用而误命中另一个数据库；缓存本身最多 8 项。
            cache_key = session.bind
            cached = self._membership_cache.get(cache_key)
            now = time.monotonic()
            if cached is not None and cached[0] > now:
                return cached[1]
            async with self._membership_lock:
                cached = self._membership_cache.get(cache_key)
                now = time.monotonic()
                if cached is not None and cached[0] > now:
                    return cached[1]
                generation = self._membership_generation
                folders = (await session.execute(select(CloudModelFolder).where(
                    CloudModelFolder.active.is_(True)
                ))).scalars().all()
                completed = (await session.execute(select(LibraryScanRun).where(
                    LibraryScanRun.status == "completed"
                ).order_by(LibraryScanRun.id.desc()).limit(1))).scalar_one_or_none()
                latest = (await session.execute(select(LibraryScanRun).order_by(
                    LibraryScanRun.id.desc()
                ).limit(1))).scalar_one_or_none()
                model_ids = frozenset(
                    int(folder.model_id) for folder in folders if folder.model_id is not None)
                normalized_names = frozenset(
                    folder.normalized_name for folder in folders if folder.normalized_name)
                folders_by_model = {
                    int(folder.model_id): self.folder_wire(folder)
                    for folder in folders if folder.model_id is not None
                }
                result = LibraryMembership(
                    model_ids=model_ids,
                    normalized_names=normalized_names,
                    folders_by_model=folders_by_model,
                    scan_generation=completed.id if completed else None,
                    last_success_at=(
                        completed.completed_at.isoformat()
                        if completed and completed.completed_at else None
                    ),
                    stale=bool(latest and latest.status == "failed"),
                )
                # 写事务可能在上面多个 await 之间提交并清空缓存。此时当前
                # session 仍可能读到旧快照；可以返回给已开始的请求，但绝不能
                # 在失效之后把旧结果重新塞回 10 秒缓存。
                if generation == self._membership_generation:
                    self._membership_cache[cache_key] = (now + 10.0, result)
                    while len(self._membership_cache) > 8:
                        self._membership_cache.pop(next(iter(self._membership_cache)))
                return result
        finally:
            if owns_session:
                await session.close()

    @staticmethod
    def folder_wire(folder: CloudModelFolder) -> dict:
        return {
            "cid": folder.cid,
            "name": folder.name,
            "normalized_name": folder.normalized_name,
            "model_id": folder.model_id,
            "match_method": folder.match_method,
            "active": bool(folder.active),
            "error": folder.error,
            "last_seen_at": (
                folder.last_seen_at.isoformat() if folder.last_seen_at else None),
        }

    async def bind(self, cid: str, model_id: int) -> dict:
        cid = str(cid or "").strip()
        async with async_session() as session:
            folder = await session.get(CloudModelFolder, cid)
            model = await session.get(Model, model_id)
            if folder is None or not folder.active:
                raise LookupError("115目录不存在或已不在当前VIDEO_HUB快照中")
            if model is None:
                raise LookupError("模特不存在")
            conflict = (await session.execute(select(CloudModelFolder).where(
                CloudModelFolder.active.is_(True),
                CloudModelFolder.model_id == model_id,
                CloudModelFolder.cid != cid,
            ))).scalar_one_or_none()
            if conflict:
                raise ValueError(f"该模特已绑定115目录：{conflict.name}")
            if folder.model_id and folder.model_id != model_id:
                old = await session.get(Model, folder.model_id)
                if old and str(old.cloud_cid or "").strip() == cid:
                    old.cloud_cid = ""
            folder.model_id = model_id
            folder.match_method = "manual"
            folder.error = ""
            model.cloud_cid = cid
            await session.commit()
            result = self.folder_wire(folder)
        self._invalidate_membership()
        await self._emit({"type": "library_membership_changed", "folder": result})
        return result

    async def unbind(self, cid: str) -> dict:
        cid = str(cid or "").strip()
        async with async_session() as session:
            folder = await session.get(CloudModelFolder, cid)
            if folder is None:
                raise LookupError("115目录不存在")
            if folder.model_id:
                model = await session.get(Model, folder.model_id)
                if model and str(model.cloud_cid or "").strip() == cid:
                    model.cloud_cid = ""
            folder.model_id = None
            folder.match_method = "unbound"
            folder.error = ""
            await session.commit()
            result = self.folder_wire(folder)
        self._invalidate_membership()
        await self._emit({"type": "library_membership_changed", "folder": result})
        return result

    async def record_binding(
        self, model_id: int, cid: str, name: str, *, session_factory=async_session,
    ):
        """上传成功后立即登记，不等待下一次115全量扫描。"""
        cid = str(cid or "").strip()
        if not cid:
            return
        now = datetime.now()
        heartbeat_before = now - timedelta(minutes=10)
        semantic_change = False
        wrote = False
        async with session_factory() as session:
            model = await session.get(Model, model_id)
            if model is None:
                return
            folder = await session.get(CloudModelFolder, cid)
            if folder is None:
                folder = CloudModelFolder(
                    cid=cid,
                    name=name or model.name,
                    normalized_name=normalize_library_name(name or model.name),
                    model_id=model_id,
                    match_method="upload",
                    active=True,
                    last_seen_at=now,
                )
                session.add(folder)
                semantic_change = True
                wrote = True
            else:
                desired_name = name or model.name
                desired_normalized = normalize_library_name(desired_name)
                semantic_change = any((
                    folder.name != desired_name,
                    folder.normalized_name != desired_normalized,
                    folder.model_id != model_id,
                    folder.match_method != "upload",
                    not folder.active,
                    bool(folder.error),
                ))
                heartbeat_due = (
                    folder.last_seen_at is None or folder.last_seen_at < heartbeat_before)
                if semantic_change or heartbeat_due:
                    folder.name = desired_name
                    folder.normalized_name = desired_normalized
                    folder.model_id = model_id
                    if semantic_change:
                        folder.match_method = "upload"
                    folder.active = True
                    folder.last_seen_at = now
                    folder.error = ""
                    wrote = True
            if str(model.cloud_cid or "").strip() != cid:
                model.cloud_cid = cid
                semantic_change = True
                wrote = True
            if wrote:
                await session.commit()
            result = self.folder_wire(folder)
        if wrote:
            self._invalidate_membership()
        if semantic_change:
            await self._emit({"type": "library_membership_changed", "folder": result})
        return result


library_index = LibraryIndex()
