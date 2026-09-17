"""目录与配额模型（8 张表）— 对齐 AgentCraft Database Design v2.0.0 §2-3。

provider_catalog / user_providers / user_quotas / user_quota_usage /
platform_slots / platform_storage / usage_daily / rate_limit_events。
枚举值常量（PROVIDER_STATUSES / SLOT_STATES）供任务域直接消费；
预留种类常量的唯一来源是 Task 5 的 tasking.py，本文件不定义任何预留种类常量。

model_capabilities 为 v0.12.4 接缝：按模型能力如实声明，形如
{"gpt-4o": {"input": ["text", "image"]}}；缺失条目视为纯文本（input=["text"]）——
阶段 3/5 生成扩展注册时消费。

PlatformSlot.task_id 由 Task 5 经 use_alter 接线为 FK → tasks.id（ondelete
SET NULL；tasks 表在 tasking.py 定义，建表后以 ALTER TABLE 补齐）。
"""

import uuid as _uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.v2.models.base import Base, TimestampMixin, check_enum, pk_uuid

PROVIDER_STATUSES = ("active", "revoked")
SLOT_STATES = ("free", "leased")


class ProviderCatalog(Base):
    __tablename__ = "provider_catalog"
    __table_args__ = (
        check_enum("provider_catalog", "healthcheck_method", ("GET", "HEAD")),
        CheckConstraint(
            "path_prefix LIKE '/%' AND path_prefix NOT LIKE '%..%'",
            name="path_prefix_shape",
        ),
        CheckConstraint(
            "healthcheck_path LIKE '/%' AND healthcheck_path NOT LIKE '%..%'",
            name="healthcheck_path_shape",
        ),
        CheckConstraint(
            "allowed_host NOT LIKE '%://%' AND allowed_host NOT LIKE '%@%'"
            " AND allowed_host NOT LIKE '%/%'",
            name="allowed_host_shape",
        ),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    allowed_host: Mapped[str] = mapped_column(String(253), nullable=False)
    path_prefix: Mapped[str] = mapped_column(String(200), nullable=False, default="/v1")
    # 模型白名单 list[str]
    models: Mapped[list] = mapped_column(JSONB, nullable=False)
    # v0.12.4 接缝：按模型能力如实声明，形如 {"gpt-4o": {"input": ["text", "image"]}}；
    # 缺失条目视为纯文本（input=["text"]）——阶段 3/5 生成扩展注册时消费
    model_capabilities: Mapped[dict | None] = mapped_column(JSONB)
    healthcheck_method: Mapped[str] = mapped_column(String(10), nullable=False, default="GET")
    healthcheck_path: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UserProvider(TimestampMixin, Base):
    __tablename__ = "user_providers"
    __table_args__ = (
        check_enum("user_providers", "status", PROVIDER_STATUSES),
        CheckConstraint("length(key_last4) = 4", name="key_last4_len"),
        Index(
            "uq_user_providers_one_default",
            "user_id",
            unique=True,
            postgresql_where=text("is_default = true"),
        ),
        # 0004：活跃条目部分唯一索引（裁决 D4）——模型/迁移双保险
        Index(
            "uq_user_providers_active_entry",
            "user_id",
            "catalog_id",
            "model_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    user_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    catalog_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("provider_catalog.id", ondelete="RESTRICT"), nullable=True
    )
    base_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    key_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    dek_wrapped: Mapped[str] = mapped_column(Text, nullable=False)
    key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class UserQuota(Base):
    __tablename__ = "user_quotas"
    user_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    max_daily_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    max_active_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_running_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_retained_storage_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1_073_741_824
    )


class UserQuotaUsage(Base):
    __tablename__ = "user_quota_usage"
    user_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    active_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    running_tasks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retained_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class PlatformSlot(Base):
    __tablename__ = "platform_slots"
    __table_args__ = (
        check_enum("platform_slots", "state", SLOT_STATES),
        Index("ix_platform_slots_state_leased_until", "state", "leased_until"),
        Index("ix_platform_slots_task_id", "task_id"),
    )
    slot_no: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # tasks 表在 tasking.py 定义；FK 于 Task 5 经 use_alter 接线（Task 5 之前为裸 Uuid 列）
    task_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", use_alter=True)
    )
    state: Mapped[str] = mapped_column(String(10), nullable=False, default="free")
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformStorage(Base):
    __tablename__ = "platform_storage"
    __table_args__ = (CheckConstraint("singleton", name="singleton_true"),)
    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    retained_storage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    max_retained_storage_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=64_424_509_440
    )


class UsageDaily(Base):
    __tablename__ = "usage_daily"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_usage_daily_user_day"),)
    id: Mapped[_uuid.UUID] = pk_uuid()
    user_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    tasks_started: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class RateLimitEvent(Base):
    __tablename__ = "rate_limit_events"
    __table_args__ = (
        Index("ix_rate_limit_scope_subject_time", "scope", "subject_hash", "occurred_at"),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    scope: Mapped[str] = mapped_column(String(60), nullable=False)
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
