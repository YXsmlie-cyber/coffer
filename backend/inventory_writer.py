"""上传确认后的库存回写；保留原事务、附属索引失败与广播顺序。"""
import logging
from sqlalchemy import select, update, func
from .database import Model, Task, Video


class InventoryWriter:
    def __init__(self, session_factory, broadcast_provider, *, binding_recorder=None, logger=None):
        self._session_factory = session_factory
        self._broadcast_provider = broadcast_provider
        self._binding_recorder = binding_recorder
        self._logger = logger or logging.getLogger(__name__)

    @property
    def _broadcast(self):
        return self._broadcast_provider()

    async def record_binding(self, model_id, cid, model_name, *, session_factory=None):
        recorder = self._binding_recorder
        if recorder is None:
            from .library_index import library_index
            recorder = library_index.record_binding
        await recorder(model_id, cid, model_name,
                       session_factory=session_factory or self._session_factory)

    async def bind_model_cid(
        self, model_id: int | None, model_name: str, cid: str
    ) -> None:
        """首次建目录后把 CID 原子回写到模特及其任务，避免并发重复建目录。"""
        if not cid:
            raise ValueError("115 模特目录 CID 为空")
        async with self._session_factory() as s:
            target_id = model_id
            if target_id is None:
                matches = (await s.execute(
                    select(Model.id).where(Model.name == model_name)
                )).scalars().all()
                if len(matches) == 1:
                    target_id = matches[0]
            if target_id is None:
                return
            await s.execute(
                update(Model).where(Model.id == target_id).values(cloud_cid=cid)
            )
            await s.execute(
                update(Task).where(
                    Task.model_id == target_id,
                    (Task.model_cloud_cid == "") | Task.model_cloud_cid.is_(None),
                ).values(model_cloud_cid=cid)
            )
            await s.commit()
        try:
            await self.record_binding(
                target_id, cid, model_name, session_factory=self._session_factory,
            )
        except Exception:
            # Model.cloud_cid 与历史 Task 已经成功落库。新索引双写失败不应阻断
            # 上传主链路；下次115目录刷新会按 CID 自动补齐。
            self._logger.exception(
                "目录绑定后更新115模特索引失败 model_id=%s cid=%s",
                target_id, cid,
            )


    async def mark_video_exists(
        self, viewkey: str, model_id: int | None, model_name: str
    ):
        """视频进入 115 后回写榜单：把对应 Video 标 exists，并重算所属 Model 的计数。

        让前端卡片缺失数立即反映现实，无需手动重新同步。
        计数用重新统计（而非增减）以免多次触发造成漂移。失败静默（不影响上传结果）。
        通过 WebSocket 广播 model_update，让模特页（若在监听）实时刷新。
        """
        try:
            async with self._session_factory() as s:
                # 精确按 model_id 回写；仅对无法迁移的旧任务退回到“唯一同名模特”。
                if model_id is not None:
                    target_ids = [model_id]
                else:
                    candidates = (await s.execute(select(Model.id).where(
                        Model.name == model_name
                    ))).scalars().all()
                    target_ids = candidates if len(candidates) == 1 else []
                payloads: list[dict] = []
                for target_id in target_ids:
                    changed = await s.execute(update(Video).where(
                        Video.model_id == target_id,
                        Video.viewkey == viewkey,
                        Video.dedup_status != "exists",
                    ).values(
                        dedup_status="exists",
                        matched_cloud_name="",
                    ))
                    if int(changed.rowcount or 0) <= 0:
                        continue
                    # 由数据库聚合三行状态计数，避免每次上传把该模特全部 Video
                    # 状态实例化到 Python；条件 UPDATE 也让重复回调天然幂等。
                    count_rows = (await s.execute(select(
                        Video.dedup_status,
                        func.count(Video.id),
                    ).where(
                        Video.model_id == target_id
                    ).group_by(Video.dedup_status))).all()
                    counts = {status: int(count) for status, count in count_rows}
                    values = {
                        "cloud_videos": counts.get("exists", 0),
                        "maybe_videos": counts.get("maybe", 0),
                        "missing_videos": counts.get("missing", 0),
                    }
                    await s.execute(update(Model).where(
                        Model.id == target_id
                    ).values(
                        **values,
                        # Video 去重状态已经被上传链路局部修改；旧快照摘要不再
                        # 描述数据库现状。置空后下次同步会比较现有行并补写新摘要，
                        # 而不是因非空旧 hash 误判内容变化后整表替换。
                        snapshot_hash="",
                    ))
                    total_videos = (await s.execute(select(
                        Model.total_videos
                    ).where(Model.id == target_id))).scalar_one_or_none()
                    payloads.append({
                        "id": target_id,
                        **values,
                        "total_videos": int(total_videos or 0),
                    })
                await s.commit()
            for p in payloads:
                if self._broadcast:
                    await self._broadcast({"type": "model_update", "model": p})
        except Exception:
            # 云端上传已经成功，回写榜单失败不能倒置上传结果；但必须留下诊断线索。
            self._logger.exception("回写视频已存在状态失败: model_id=%s viewkey=%s", model_id, viewkey)
