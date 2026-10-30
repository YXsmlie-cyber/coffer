"""SQLAlchemy 异步数据库定义与模型。"""
from datetime import datetime
from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, ForeignKey, LargeBinary, Text,
    UniqueConstraint, Index, Computed, event, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from .config import settings
from .task_contracts import TASK_STATUS_SQL
from .identity import normalize_library_name as _normalize_model_name_key

engine = create_async_engine(f"sqlite+aiosqlite:///{settings.db_path}", echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _connection_record):
    """每条连接启用外键与合理的锁等待，避免静默孤儿行和瞬时锁失败。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
    finally:
        cursor.close()


class Base(DeclarativeBase):
    pass


class RuntimeSetting(Base):
    """需要在进程重启后保留的少量运行时设置。"""
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now)


class Model(Base):
    """可同步的 PH 上传者；历史名称沿用 Model 以保持任务/外键兼容。"""
    __tablename__ = "models"
    __table_args__ = (
        Index("ix_models_profile_rank_order_id", "profile_type", "rank", "scrape_order", "id"),
        Index("ix_models_profile_missing_id", "profile_type", "missing_videos", "id"),
        Index("ix_models_profile_name_id", "profile_type", "name", "id"),
        Index("ix_models_profile_library_name_id",
              "profile_type", "library_name_key", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # performer 沿用裸 slug；频道/用户使用 ``channel:slug`` / ``user:slug``，
    # 避免不同主页类型的同名 slug 撞上历史唯一约束。
    ph_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    # model / pornstar / channel / user。旧库在轻量迁移中按 URL 回填。
    profile_type: Mapped[str] = mapped_column(String, default="model", index=True)
    profile_slug: Mapped[str] = mapped_column(String, default="", index=True)
    name: Mapped[str] = mapped_column(String, index=True)
    library_name_key: Mapped[str] = mapped_column(
        String, default="", server_default="", index=True)
    url: Mapped[str] = mapped_column(String)
    # thumbnail 是旧版远程地址，仅为回滚兼容保留；页面展示使用本地 avatar_sha256。
    thumbnail: Mapped[str] = mapped_column(String, default="")
    avatar_source_url: Mapped[str] = mapped_column(String, default="")
    avatar_sha256: Mapped[str] = mapped_column(String, default="")
    avatar_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # ready / source_no_avatar / source_unavailable / pending
    avatar_status: Mapped[str] = mapped_column(String, default="pending")
    avatar_error: Mapped[str] = mapped_column(Text, default="")
    avatar_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cloud_cid: Mapped[str] = mapped_column(String, default="")  # 115对应文件夹cid
    rank: Mapped[int] = mapped_column(Integer, default=0, index=True)  # 榜单名次(目标站排名)，0=未知
    scrape_order: Mapped[int] = mapped_column(Integer, default=0)  # 爬取顺序，rank缺失时兜底排序
    ph_video_count: Mapped[int] = mapped_column(Integer, default=0)  # 榜单页显示的视频数
    views_text: Mapped[str] = mapped_column(String, default="")  # 榜单页观看数原文，如 "2.2B"
    total_videos: Mapped[int] = mapped_column(Integer, default=0)  # PH上视频总数
    cloud_videos: Mapped[int] = mapped_column(Integer, default=0)  # 115已有数
    maybe_videos: Mapped[int] = mapped_column(Integer, default=0)  # 弱匹配(疑似已有)数
    missing_videos: Mapped[int] = mapped_column(Integer, default=0)  # 缺失数
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 完整视频快照的版本化稳定摘要；用于同步无变化时跳过大批量 DELETE/INSERT。
    snapshot_hash: Mapped[str] = mapped_column(
        String, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    videos: Mapped[list["Video"]] = relationship(back_populates="model", cascade="all, delete-orphan")
    discovery_items: Mapped[list["DiscoveryItem"]] = relationship(
        back_populates="model", cascade="all, delete-orphan")


@event.listens_for(Model, "before_insert")
@event.listens_for(Model, "before_update")
def _sync_model_library_name_key(_mapper, _connection, target: Model):
    target.library_name_key = _normalize_model_name_key(target.name)


class Video(Base):
    """单个视频（来自模特视频列表爬取）。"""
    __tablename__ = "videos"
    # PH 一个 viewkey 可能出现在多个模特页（合作视频/转载），所以 viewkey 不能全局唯一，
    # 但同一个模特库里同 viewkey 不应重复，用复合唯一约束。
    __table_args__ = (
        UniqueConstraint("model_id", "viewkey", name="uq_video_model_viewkey"),
        Index("ix_videos_model_dedup_status", "model_id", "dedup_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id"), index=True)
    viewkey: Mapped[str] = mapped_column(String, index=True)  # PH唯一标识（每个模特库内）
    title: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    thumbnail: Mapped[str] = mapped_column(String, default="")
    duration: Mapped[int] = mapped_column(Integer, default=0)  # 秒
    # 源站列表中的稳定顺序；旧快照为 0，预览服务会按行 id 兜底。
    source_order: Mapped[int] = mapped_column(Integer, default=0)
    # 去重状态: exists(强匹配) / maybe(时长弱匹配) / missing(缺失)
    dedup_status: Mapped[str] = mapped_column(String, default="missing", index=True)
    matched_cloud_name: Mapped[str] = mapped_column(String, default="")  # 弱匹配命中的115文件名

    model: Mapped["Model"] = relationship(back_populates="videos")


class DiscoveryItem(Base):
    """某个视频来源快照中的展示项；与完整同步后的 Video 快照相互独立。"""
    __tablename__ = "discovery_items"
    __table_args__ = (
        Index("ix_discovery_source_order", "source", "source_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, index=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True)
    viewkey: Mapped[str] = mapped_column(String, default="", index=True)
    title: Mapped[str] = mapped_column(String, default="")
    url: Mapped[str] = mapped_column(String, default="")
    thumbnail: Mapped[str] = mapped_column(String, default="")
    duration: Mapped[int] = mapped_column(Integer, default=0)
    source_order: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    model: Mapped["Model"] = relationship(back_populates="discovery_items")


class LibraryScanRun(Base):
    """一次 VIDEO_HUB 一级目录快照；只有 completed 才可成为当前权威代次。"""
    __tablename__ = "library_scan_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String, default="running", index=True)
    folder_count: Mapped[int] = mapped_column(Integer, default=0)
    bound_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CloudModelFolder(Base):
    """VIDEO_HUB 下的目录身份；未绑定目录也必须持久化并可审计。"""
    __tablename__ = "cloud_model_folders"
    __table_args__ = (
        Index("ix_cloud_model_folders_normalized", "normalized_name"),
        Index("ix_cloud_model_folders_active_model", "active", "model_id"),
        Index("ix_cloud_model_folders_active_normalized",
              "active", "normalized_name"),
    )

    cid: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    normalized_name: Mapped[str] = mapped_column(String)
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL"), nullable=True,
        unique=True, index=True,
    )
    match_method: Mapped[str] = mapped_column(String, default="unbound")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    scan_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_scan_runs.id", ondelete="SET NULL"), nullable=True,
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    error: Mapped[str] = mapped_column(Text, default="")


class ModelAlias(Base):
    """人工确认的可信别名；归一化别名全局唯一，冲突必须人工处理。"""
    __tablename__ = "model_aliases"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String)
    normalized_alias: Mapped[str] = mapped_column(String, unique=True, index=True)
    trusted: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SourceSnapshot(Base):
    """候选来源快照；失败时保留上一次成功快照供原子推荐任务复用。"""
    __tablename__ = "source_snapshots"
    __table_args__ = (Index("ix_source_snapshot_status", "source", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, index=True)
    status: Mapped[str] = mapped_column(String, default="completed")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    stale: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Candidate(Base):
    """来源发现的候选；解析到唯一 PH 身份后才允许进入可操作推荐池。"""
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    candidate_key: Mapped[str] = mapped_column(String, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String, index=True)
    normalized_name: Mapped[str] = mapped_column(String, index=True)
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), nullable=True,
        unique=True, index=True,
    )
    entity_type: Mapped[str] = mapped_column(String, default="performer", index=True)
    gender_status: Mapped[str] = mapped_column(
        String, default="needs_review", index=True)
    resolution_status: Mapped[str] = mapped_column(
        String, default="unresolved", index=True)
    lifecycle_status: Mapped[str] = mapped_column(
        String, default="active", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now)


class CandidateSourceHit(Base):
    """一个候选在一个来源中的可追溯命中。"""
    __tablename__ = "candidate_source_hits"
    __table_args__ = (
        UniqueConstraint("source", "source_identity", name="uq_candidate_source_identity"),
        Index("ix_candidate_source_rank", "source", "source_rank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_snapshots.id", ondelete="SET NULL"), nullable=True)
    source: Mapped[str] = mapped_column(String, index=True)
    source_identity: Mapped[str] = mapped_column(String)
    source_url: Mapped[str] = mapped_column(String, default="")
    source_rank: Mapped[int] = mapped_column(Integer, default=0)
    signal_type: Mapped[str] = mapped_column(String, default="discovered")
    trust_score: Mapped[float] = mapped_column(Float, default=0.0)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CandidateSourceObservation(Base):
    """来源快照中的不可变候选观察，用于计算真实排名变化而不是覆盖历史。"""
    __tablename__ = "candidate_source_observations"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "source", "source_identity",
            name="uq_candidate_source_observation",
        ),
        Index(
            "ix_candidate_source_observed",
            "candidate_id", "source", "observed_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("source_snapshots.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String, index=True)
    source_identity: Mapped[str] = mapped_column(String)
    source_rank: Mapped[int] = mapped_column(Integer, default=0)
    signal_type: Mapped[str] = mapped_column(String, default="discovered")
    trust_score: Mapped[float] = mapped_column(Float, default=0.0)
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ModelPreviewVideo(Base):
    """页面直接读取的每模特最多四条预览，避免列表请求全扫视频表。"""
    __tablename__ = "model_preview_videos"
    __table_args__ = (
        UniqueConstraint("model_id", "viewkey", name="uq_preview_model_viewkey"),
    )

    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    viewkey: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    url: Mapped[str] = mapped_column(String, default="")
    thumbnail: Mapped[str] = mapped_column(String, default="")
    duration: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String, default="video")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PreferenceFeedback(Base):
    """单用户显式偏好事件；被撤销的历史事件保留但 active=False。"""
    __tablename__ = "preference_feedback"
    __table_args__ = (Index("ix_feedback_candidate_active", "candidate_id", "active"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=True, index=True)
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String, index=True)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String, default="manual")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TasteComparison(Base):
    """审美校准的两两比较事件；与旧推荐反馈分表，保留明确的左右语义。"""
    __tablename__ = "taste_comparisons"
    __table_args__ = (
        UniqueConstraint("pair_key", name="uq_taste_comparison_pair"),
        Index("ix_taste_comparison_created", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pair_key: Mapped[str] = mapped_column(String, nullable=False)
    left_model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True)
    right_model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True)
    # left / right / both / neither / skip
    choice: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class FeatureVector(Base):
    """可版本化的本地特征向量；BLOB 内容为小端 Float32。"""
    __tablename__ = "feature_vectors"
    __table_args__ = (
        UniqueConstraint(
            "model_id", "feature_type", "item_key", "model_version",
            name="uq_feature_vector_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True)
    feature_type: Mapped[str] = mapped_column(String, index=True)
    item_key: Mapped[str] = mapped_column(String, default="model")
    model_version: Mapped[str] = mapped_column(String)
    input_hash: Mapped[str] = mapped_column(String, default="", index=True)
    dimensions: Mapped[int] = mapped_column(Integer, default=0)
    vector: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String, default="ready", index=True)
    error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TasteModelRun(Base):
    """一次审美视觉验证/排行代次；与已下线的旧推荐代次完全隔离。"""
    __tablename__ = "taste_model_runs"
    __table_args__ = (
        Index("ix_taste_model_run_created", "created_at", "id"),
        Index("ix_taste_model_run_published", "published", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    comparison_hash: Mapped[str] = mapped_column(String, index=True)
    model_version: Mapped[str] = mapped_column(String, default="")
    feature_mode: Mapped[str] = mapped_column(String, default="avatar_first_cover_50_50")
    # validating / validated / fetching_covers / indexing / scoring / ready / quality_failed / failed
    status: Mapped[str] = mapped_column(String, default="validating", index=True)
    quality_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    eligible_count: Mapped[int] = mapped_column(Integer, default=0)
    scored_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TasteModelScore(Base):
    """审美视觉代次对剩余候选的统一排行，不包含任何任务副作用。"""
    __tablename__ = "taste_model_scores"
    __table_args__ = (
        Index("ix_taste_model_score_rank", "run_id", "rank"),
    )

    run_id: Mapped[int] = mapped_column(
        ForeignKey("taste_model_runs.id", ondelete="CASCADE"), primary_key=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TasteResultFeedback(Base):
    """首批结果上的单模特显式反馈；只训练后续审美模型，不创建任务。"""
    __tablename__ = "taste_result_feedback"
    __table_args__ = (
        Index("ix_taste_feedback_model_active_created", "model_id", "active", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("taste_model_runs.id", ondelete="SET NULL"), nullable=True)
    # like / dislike
    action: Mapped[str] = mapped_column(String, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RecommendationRun(Base):
    """完整推荐计算代次；仅一个 completed 代次会被标记 published。"""
    __tablename__ = "recommendation_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    algorithm_version: Mapped[str] = mapped_column(String)
    library_scan_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_scan_runs.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="running", index=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_state_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RecommendationScore(Base):
    """一次推荐代次中某候选在一个榜单的稳定排序与可解释分项。"""
    __tablename__ = "recommendation_scores"
    __table_args__ = (
        Index("ix_recommendation_list_rank", "run_id", "list_type", "rank"),
    )

    run_id: Mapped[int] = mapped_column(
        ForeignKey("recommendation_runs.id", ondelete="CASCADE"), primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), primary_key=True)
    list_type: Mapped[str] = mapped_column(String, primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    components_json: Mapped[str] = mapped_column(Text, default="{}")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")


class Task(Base):
    """下载 / 上传任务。"""
    __tablename__ = "tasks"
    __table_args__ = (
        Index(
            "uq_task_model_viewkey_type",
            "model_id", "viewkey", "task_type",
            unique=True,
            sqlite_where=text("model_id IS NOT NULL"),
        ),
        Index("ix_tasks_schedule_type_status_created_id",
              "task_type", "status", "created_at", "id"),
        Index("ix_tasks_type_status_rank_id_desc",
              "task_type", "status_rank", text("id DESC")),
        Index("ix_tasks_status_rank_id_desc", "status_rank", text("id DESC")),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    viewkey: Mapped[str] = mapped_column(String, index=True)
    # 任务身份必须包含模特；合作视频可在多个模特目录各自形成上传任务。
    # 允许 NULL 以保留删除模特后的历史任务，以及兼容无法唯一回填的旧数据。
    model_id: Mapped[int | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL"), nullable=True, index=True)
    model_name: Mapped[str] = mapped_column(String)
    model_cloud_cid: Mapped[str] = mapped_column(String, default="")
    # 旧任务为空，继续沿用 downloads/<model>/<viewkey> 以保留断点；
    # 新任务写 model_id，使跨模特同 viewkey 使用互不冲突的本地目录。
    artifact_scope: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    duration: Mapped[int] = mapped_column(Integer, default=0)
    # 类型: download / upload
    task_type: Mapped[str] = mapped_column(String, default="download", index=True)
    # 状态: queued / running / paused / completed / failed / skipped
    status: Mapped[str] = mapped_column(String, default="queued", index=True)
    # 任务列表的稳定优先级必须是实际列/表达式索引，而不是每次查询临时构造 CASE；
    # VIRTUAL generated column 不占用每行额外存储，旧库也可安全 ADD COLUMN。
    status_rank: Mapped[int] = mapped_column(Integer, Computed(TASK_STATUS_SQL, persisted=False))
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0-100
    speed: Mapped[str] = mapped_column(String, default="")  # 显示用速度
    # 下载阶段与结构化进度；speed 仅保留为旧版前端兼容显示字段。
    phase: Mapped[str] = mapped_column(String, default="")
    speed_bps: Mapped[float] = mapped_column(Float, default=0.0)
    downloaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    last_progress_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 上传失败自动重试必须持久化，否则进程重启会把次数清零并可能无限重试。
    auto_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 上传已结束但巡查仍判 missing 时，连续确认两轮才允许重下，吸收 115 列表延迟。
    remote_missing_count: Mapped[int] = mapped_column(Integer, default=0)
    # 巡查因本地文件丢失/远端漂移而重置下载的次数，防止异常数据无限循环。
    patrol_recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    # 下载任务在 worker 启动前即写入目标绝对路径；完成后上传任务沿用该路径。
    # 这样多下载盘切换、重启续传和上传清理始终指向同一份本地产物。
    local_path: Mapped[str] = mapped_column(String, default="")
    m3u8_url: Mapped[str] = mapped_column(String, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    # 只追踪本次操作新建/重新入队的任务，供“撤销追加”安全取消；旧任务保持空值。
    origin: Mapped[str] = mapped_column(String, default="", index=True)
    origin_candidate_id: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


async def init_db():
    async with engine.begin() as conn:
        await _enable_sqlite_wal(conn)
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)


async def _enable_sqlite_wal(conn) -> str:
    """启用 WAL，让页面读取与短任务状态写入可以并行。"""
    result = await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
    return str(result.scalar_one()).casefold()


_SCHEMA_MIGRATION_VERSION = 6


async def _migrate(conn):
    """轻量迁移：本项目无迁移框架，create_all 不会给已存在的表补列。
    这里用幂等的 ALTER TABLE ADD COLUMN 补齐新列，保留既有 data.db 数据。
    列名为硬编码常量，无注入风险。
    """

    async def add_missing_columns(table: str, columns: dict[str, str]):
        # table_info 会隐藏 generated/hidden columns；若一次迁移在加入
        # status_rank 后中断，重试会误判缺列并触发 duplicate column。
        rows = await conn.exec_driver_sql(f"PRAGMA table_xinfo({table})")
        existing = {r[1] for r in rows.fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    async def table_exists(table: str) -> bool:
        row = await conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
        return row.first() is not None

    await conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS runtime_settings (
            key VARCHAR PRIMARY KEY,
            value TEXT DEFAULT '',
            updated_at DATETIME
        )
    """)
    version_row = await conn.exec_driver_sql(
        "SELECT value FROM runtime_settings WHERE key = 'schema_migration_version'")
    try:
        migration_version = int(version_row.scalar_one_or_none() or 0)
    except (TypeError, ValueError):
        migration_version = 0
    # 数据修复、ROW_NUMBER 去重和 ANALYZE 都只应在显式版本升级时运行。
    # 老库没有版本记录，会完整跑一次；中途失败不会写版本，下次仍可幂等续跑。
    if migration_version >= _SCHEMA_MIGRATION_VERSION:
        return
    # 新表首次创建若在某个索引处中断，下一次启动仍可幂等补齐关键查询索引。
    if await table_exists("taste_result_feedback"):
        await conn.exec_driver_sql("""
            CREATE INDEX IF NOT EXISTS ix_taste_feedback_model_active_created
                ON taste_result_feedback (model_id, active, created_at)
        """)

    await add_missing_columns("models", {
        "profile_type": "VARCHAR NOT NULL DEFAULT 'model'",
        "profile_slug": "VARCHAR NOT NULL DEFAULT ''",
        "rank": "INTEGER DEFAULT 0",
        "scrape_order": "INTEGER DEFAULT 0",
        "maybe_videos": "INTEGER DEFAULT 0",
        "missing_videos": "INTEGER DEFAULT 0",
        "total_videos": "INTEGER DEFAULT 0",
        "cloud_videos": "INTEGER DEFAULT 0",
        "ph_video_count": "INTEGER DEFAULT 0",
        "views_text": "VARCHAR DEFAULT ''",
        "avatar_source_url": "VARCHAR NOT NULL DEFAULT ''",
        "avatar_sha256": "VARCHAR NOT NULL DEFAULT ''",
        "avatar_updated_at": "DATETIME",
        "avatar_status": "VARCHAR NOT NULL DEFAULT 'pending'",
        "avatar_error": "TEXT NOT NULL DEFAULT ''",
        "avatar_checked_at": "DATETIME",
        "snapshot_hash": "VARCHAR NOT NULL DEFAULT ''",
        "library_name_key": "VARCHAR NOT NULL DEFAULT ''",
    })
    model_info = await conn.exec_driver_sql("PRAGMA table_info(models)")
    model_columns = {row[1] for row in model_info.fetchall()}
    if "url" in model_columns:
        await conn.exec_driver_sql("""
            UPDATE models
               SET profile_type = CASE
                   WHEN lower(url) LIKE '%/pornstar/%' THEN 'pornstar'
                   WHEN lower(url) LIKE '%/channels/%' THEN 'channel'
                   WHEN lower(url) LIKE '%/users/%' THEN 'user'
                   ELSE 'model' END
             WHERE profile_type IS NULL OR profile_type = ''
                OR (profile_type = 'model' AND (
                       lower(url) LIKE '%/pornstar/%'
                    OR lower(url) LIKE '%/channels/%'
                    OR lower(url) LIKE '%/users/%'
                ))
        """)
    if "ph_id" in model_columns:
        await conn.exec_driver_sql("""
            UPDATE models
               SET profile_slug = CASE
                   WHEN instr(ph_id, ':') > 0 THEN substr(ph_id, instr(ph_id, ':') + 1)
                   ELSE ph_id END
             WHERE profile_slug IS NULL OR profile_slug = ''
        """)
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_models_profile_type ON models (profile_type)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_models_profile_slug ON models (profile_slug)")
    await conn.exec_driver_sql("""
        UPDATE models
           SET rank = coalesce(rank, 0),
               scrape_order = coalesce(scrape_order, 0),
               maybe_videos = coalesce(maybe_videos, 0),
               missing_videos = coalesce(missing_videos, 0),
               ph_video_count = coalesce(ph_video_count, 0),
               total_videos = coalesce(total_videos, 0),
               cloud_videos = coalesce(cloud_videos, 0)
         WHERE rank IS NULL OR scrape_order IS NULL OR maybe_videos IS NULL
            OR missing_videos IS NULL OR ph_video_count IS NULL
            OR total_videos IS NULL OR cloud_videos IS NULL
    """)
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_models_profile_rank_order_id "
        "ON models (profile_type, rank, scrape_order, id)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_models_profile_missing_id "
        "ON models (profile_type, missing_videos, id)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_models_profile_name_id "
        "ON models (profile_type, name, id)")
    model_name_rows = (await conn.exec_driver_sql(
        "SELECT id, name, library_name_key FROM models"
    )).fetchall()
    name_updates = [
        (_normalize_model_name_key(row[1]), int(row[0]))
        for row in model_name_rows
        if str(row[2] or "") != _normalize_model_name_key(row[1])
    ]
    if name_updates:
        await conn.exec_driver_sql(
            "UPDATE models SET library_name_key = ? WHERE id = ?", name_updates)
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_models_profile_library_name_id "
        "ON models (profile_type, library_name_key, id)")
    await conn.exec_driver_sql("""
        UPDATE models
           SET avatar_status = 'ready', avatar_error = ''
         WHERE length(coalesce(avatar_sha256, '')) = 64
           AND avatar_status != 'ready'
    """)
    await add_missing_columns("videos", {
        "model_id": "INTEGER",
        "thumbnail": "VARCHAR DEFAULT ''",
        "duration": "INTEGER DEFAULT 0",
        "source_order": "INTEGER DEFAULT 0",
        "dedup_status": "VARCHAR DEFAULT 'missing'",
        "matched_cloud_name": "VARCHAR DEFAULT ''",
    })
    await add_missing_columns("tasks", {
        "model_id": "INTEGER",
        "model_cloud_cid": "VARCHAR DEFAULT ''",
        "artifact_scope": "VARCHAR NOT NULL DEFAULT ''",
        "duration": "INTEGER DEFAULT 0",
        "task_type": "VARCHAR DEFAULT 'download'",
        "status": "VARCHAR DEFAULT 'queued'",
        "status_rank": f"INTEGER GENERATED ALWAYS AS ({TASK_STATUS_SQL}) VIRTUAL",
        "progress": "FLOAT DEFAULT 0",
        "speed": "VARCHAR DEFAULT ''",
        "phase": "VARCHAR NOT NULL DEFAULT ''",
        "speed_bps": "FLOAT NOT NULL DEFAULT 0",
        "downloaded_bytes": "INTEGER NOT NULL DEFAULT 0",
        "total_bytes": "INTEGER NOT NULL DEFAULT 0",
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "last_progress_at": "DATETIME",
        "auto_retry_count": "INTEGER NOT NULL DEFAULT 0",
        "next_retry_at": "DATETIME",
        "remote_missing_count": "INTEGER NOT NULL DEFAULT 0",
        "patrol_recovery_count": "INTEGER NOT NULL DEFAULT 0",
        "local_path": "VARCHAR DEFAULT ''",
        "m3u8_url": "VARCHAR DEFAULT ''",
        "error": "TEXT DEFAULT ''",
        "origin": "VARCHAR NOT NULL DEFAULT ''",
        "origin_candidate_id": "INTEGER",
        "created_at": "DATETIME",
        "updated_at": "DATETIME",
    })

    # 先按非空 CID 唯一匹配，再按唯一模特名回填旧任务。重名且无 CID 的旧行
    # 故意保留 NULL，避免把历史任务绑定到错误模特。
    await conn.exec_driver_sql("""
        UPDATE tasks
           SET model_id = (
               SELECT MIN(m.id) FROM models AS m
                WHERE m.cloud_cid != '' AND m.cloud_cid = tasks.model_cloud_cid
           )
         WHERE model_id IS NULL
           AND model_cloud_cid != ''
           AND 1 = (
               SELECT COUNT(*) FROM models AS m
                WHERE m.cloud_cid != '' AND m.cloud_cid = tasks.model_cloud_cid
           )
    """)
    await conn.exec_driver_sql("""
        UPDATE tasks
           SET model_id = (
               SELECT MIN(m.id) FROM models AS m WHERE m.name = tasks.model_name
           )
         WHERE model_id IS NULL
           AND 1 = (
               SELECT COUNT(*) FROM models AS m WHERE m.name = tasks.model_name
           )
    """)
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_tasks_model_id ON tasks (model_id)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_tasks_origin ON tasks (origin)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_tasks_origin_candidate_id "
        "ON tasks (origin_candidate_id)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_tasks_schedule_type_status_created_id "
        "ON tasks (task_type, status, created_at, id)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_tasks_type_status_rank_id_desc "
        "ON tasks (task_type, status_rank, id DESC)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_tasks_status_rank_id_desc "
        "ON tasks (status_rank, id DESC)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_videos_model_dedup_status "
        "ON videos (model_id, dedup_status)")
    if await table_exists("cloud_model_folders"):
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cloud_model_folders_active_model "
            "ON cloud_model_folders (active, model_id)")
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_cloud_model_folders_active_normalized "
            "ON cloud_model_folders (active, normalized_name)")
    # 极旧版本曾允许并发查重后插入同一逻辑任务。创建唯一索引前保留每组
    # 最可信的一行，其余行解除 model_id 绑定；活跃重复项改为 failed，避免
    # restore/resume 后又执行一次。历史行仍保留在任务列表供人工核对。
    await conn.exec_driver_sql("""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY model_id, viewkey, task_type
                       ORDER BY CASE status
                           WHEN 'completed' THEN 0
                           WHEN 'skipped' THEN 1
                           WHEN 'running' THEN 2
                           WHEN 'queued' THEN 3
                           WHEN 'paused' THEN 4
                           WHEN 'failed' THEN 5
                           ELSE 6 END,
                           id DESC
                   ) AS duplicate_rank
              FROM tasks
             WHERE model_id IS NOT NULL
        )
        UPDATE tasks
           SET model_id = NULL,
               status = CASE
                   WHEN status IN ('running', 'queued', 'paused') THEN 'failed'
                   ELSE status END,
               error = CASE
                   WHEN status IN ('running', 'queued', 'paused')
                   THEN substr(coalesce(error, '') || ' [迁移：重复任务已解除模特绑定]', 1, 500)
                   ELSE error END
         WHERE id IN (SELECT id FROM ranked WHERE duplicate_rank > 1)
    """)
    await conn.exec_driver_sql("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_task_model_viewkey_type
            ON tasks (model_id, viewkey, task_type)
         WHERE model_id IS NOT NULL
    """)
    await conn.exec_driver_sql("""
        INSERT INTO runtime_settings(key, value, updated_at)
        VALUES ('schema_migration_version', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
    """, (str(_SCHEMA_MIGRATION_VERSION),))
    # 只在索引/结构升级后更新统计信息；不做启动时 VACUUM，也不在每次启动
    # 重复扫描大表。PRAGMA optimize 由 SQLite 根据需要执行低成本维护。
    await conn.exec_driver_sql("ANALYZE")
    await conn.exec_driver_sql("PRAGMA optimize")
