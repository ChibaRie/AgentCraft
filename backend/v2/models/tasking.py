"""任务域模型（7 张表）— 对齐 AgentCraft Database Design v2.0.0 §2-3。

tasks / task_files / task_reservations / task_messages / task_rounds /
task_events / idempotency_records。
枚举值常量（TASK_STATUSES / ROUND_STATES / RESERVATION_* / FILE_STATES /
MESSAGE_AUTHORS / EVENT_TYPES）为全仓唯一来源，供阶段 2 任务域直接消费。

循环 FK（use_alter=True，建表后以 ALTER TABLE 补齐）：
- tasks.initial_message_id → task_messages.id（SET NULL）：与
  task_messages.task_id（普通 CASCADE FK）互指；
- platform_slots.task_id → tasks.id（SET NULL）：Task 5 在 catalog.py 接线。

tasks.owner_id 为硬 FK（users.id CASCADE）；task_files / task_messages /
task_rounds / task_events / task_reservations 的 owner_id 为裸 Uuid NOT NULL
（插入时写 tasks.owner_id，之后不可改——Task 6 直接 RLS 用，不设 FK）；
tasks.provider_catalog_id / task_events.message_id / task_events.round_id
同为裸 Uuid（快照 / 弱引用，不设 FK）。

部分唯一索引与唯一约束名与 Database Design §3 一字不差（模型侧与迁移侧双保险）：
one_active_round_per_task / round_source_message / task_event_sequence /
task_message_event_sequence / idempotency_route_key /
one_live_active_reservation / one_live_running_reservation /
one_live_task_root_reservation / one_live_artifact_copy_reservation。
"""
import uuid as _uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.v2.models.base import Base, TimestampMixin, check_enum, pk_uuid

TASK_STATUSES = (
    "uploading", "queued", "running", "ready", "completed", "failed", "aborted", "deleted",
)
ROUND_STATES = ("pending", "running", "cancelling", "settled", "failed", "cancelled")
RESERVATION_STATES = ("held", "consumed", "released")
RESERVATION_KINDS = ("active", "running", "task_root", "artifact_copy")
FILE_STATES = ("staged", "committed", "registered", "deleted")
MESSAGE_AUTHORS = ("user", "assistant", "tool")
EVENT_TYPES = (
    "message_saved", "round_queued", "round_running", "round_settled",
    "round_failed", "round_cancelled", "status_changed",
)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        check_enum("tasks", "status", TASK_STATUSES),
        Index("ix_tasks_owner_created", "owner_id", "created_at"),  # DB §5: owner_id, created_at
        Index("ix_tasks_id_owner", "id", "owner_id"),  # DB §5: id, owner_id
        Index(
            "ix_tasks_queued",
            "status",
            "created_at",
            postgresql_where=text("status = 'queued'"),  # DB §5: 队列调度 partial
        ),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    owner_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expert_revision_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("expert_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    provider_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("user_providers.id", ondelete="RESTRICT"), nullable=False
    )
    # 快照列：裸 Uuid，无 FK（目录可能被清理，任务保留当时的目录指针）
    provider_catalog_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 循环 FK：建表经 use_alter 以 ALTER TABLE 补齐；消息删除时指针置空
    initial_message_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("task_messages.id", ondelete="SET NULL", use_alter=True)
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="uploading")
    input_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    input_committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    abort_reason: Mapped[str | None] = mapped_column(String(60))


class TaskFile(Base):
    __tablename__ = "task_files"
    __table_args__ = (
        check_enum("task_files", "direction", ("input", "output")),
        check_enum("task_files", "state", FILE_STATES),
        CheckConstraint(
            "position('/' in file_name) = 0 AND position('\\' in file_name) = 0",
            name="file_name_single_segment",
        ),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    task_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 冗余 owner 列：插入时写 tasks.owner_id，之后不可改——Task 6 直接 RLS 用（不设 FK）
    owner_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)  # 展示名，单段
    storage_key: Mapped[str] = mapped_column(String(200), nullable=False)  # tasks/<uuid>/<uuid>
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="staged")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskReservation(Base):
    __tablename__ = "task_reservations"
    __table_args__ = (
        check_enum("task_reservations", "kind", RESERVATION_KINDS),
        check_enum("task_reservations", "state", RESERVATION_STATES),
        Index(
            "one_live_active_reservation",
            "task_id",
            unique=True,
            postgresql_where=text("kind = 'active' AND state IN ('held','consumed')"),
        ),
        Index(
            "one_live_running_reservation",
            "task_id",
            unique=True,
            postgresql_where=text("kind = 'running' AND state IN ('held','consumed')"),
        ),
        Index(
            "one_live_task_root_reservation",
            "task_id",
            unique=True,
            postgresql_where=text("kind = 'task_root' AND state IN ('held','consumed')"),
        ),
        Index(
            "one_live_artifact_copy_reservation",
            "task_id",
            "file_id",
            unique=True,
            postgresql_where=text("kind = 'artifact_copy' AND state IN ('held','consumed')"),
        ),
        Index("ix_task_reservations_user_kind_state", "user_id", "kind", "state"),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    task_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余 owner 列：插入时写 tasks.owner_id，之后不可改——Task 6 直接 RLS 用（不设 FK）
    user_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    file_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("task_files.id", ondelete="CASCADE")
    )
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="held")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TaskMessage(Base):
    __tablename__ = "task_messages"
    __table_args__ = (
        UniqueConstraint("task_id", "event_sequence", name="task_message_event_sequence"),
        check_enum("task_messages", "author", MESSAGE_AUTHORS),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    task_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余 owner 列：插入时写 tasks.owner_id，之后不可改——Task 6 直接 RLS 用（不设 FK）
    owner_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskRound(TimestampMixin, Base):
    __tablename__ = "task_rounds"
    __table_args__ = (
        check_enum("task_rounds", "state", ROUND_STATES),
        Index(
            "one_active_round_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("state IN ('pending','running','cancelling')"),
        ),
        UniqueConstraint("source_message_id", name="round_source_message"),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    task_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余 owner 列：插入时写 tasks.owner_id，之后不可改——Task 6 直接 RLS 用（不设 FK）
    owner_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_message_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("task_messages.id", ondelete="RESTRICT"), nullable=False
    )
    client_request_id: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="task_event_sequence"),
        check_enum("task_events", "type", EVENT_TYPES),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    task_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余 owner 列：插入时写 tasks.owner_id，之后不可改——Task 6 直接 RLS 用（不设 FK）
    owner_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSONB)
    message_id: Mapped[_uuid.UUID | None] = mapped_column(Uuid)  # 弱引用：不设 FK
    round_id: Mapped[_uuid.UUID | None] = mapped_column(Uuid)  # 弱引用：不设 FK
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("subject_hash", "route", "key", name="idempotency_route_key"),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    route: Mapped[str] = mapped_column(String(300), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict | None] = mapped_column(JSONB)
    status_code: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
