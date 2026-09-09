"""内容与治理模型（9 张表）— 对齐 AgentCraft Database Design v2.0.0 §2-3。

experts / expert_revisions / skills / skill_revisions / tool_catalog /
revision_tools / content_reviews / reports / audit_logs。
枚举值常量（REVISION_STATUSES / ENTITY_STATUSES / REPORT_STATUSES）供
治理域直接消费；预留事件常量的唯一来源是 Task 5 的 tasking.py，
本文件不定义任何预留事件常量。

循环 FK：experts.published_revision_id → expert_revisions.id 与
skills.published_revision_id → skill_revisions.id 均以 use_alter=True 声明
（建表后以 ALTER TABLE 补齐），ondelete="SET NULL"；expert_revisions /
skill_revisions 携带冗余 owner_id 裸列（插入时写 parents.owner_id，之后
不可改——Task 6 直接 RLS 用，不设 FK）。content_reviews.target_revision_id
为多态引用（expert_revision/skill_revision），同样不设硬 FK。
"""
import uuid as _uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.v2.models.base import Base, TimestampMixin, check_enum, pk_uuid

REVISION_STATUSES = (
    "draft", "pending_review", "approved", "rejected", "published", "archived",
)
ENTITY_STATUSES = ("draft", "published", "archived")
REPORT_STATUSES = ("open", "dismissed", "actioned")


class Expert(TimestampMixin, Base):
    __tablename__ = "experts"
    __table_args__ = (check_enum("experts", "status", ENTITY_STATUSES),)
    id: Mapped[_uuid.UUID] = pk_uuid()
    owner_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 循环 FK：建表经 use_alter 以 ALTER TABLE 补齐；revision 删除时指针置空
    # （Uuid 类型由 Mapped[UUID | None] 注解推断）
    published_revision_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("expert_revisions.id", ondelete="SET NULL", use_alter=True)
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")


class ExpertRevision(TimestampMixin, Base):
    __tablename__ = "expert_revisions"
    __table_args__ = (
        UniqueConstraint("expert_id", "revision_no", name="expert_revision_no"),
        check_enum("expert_revisions", "status", REVISION_STATUSES),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    expert_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("experts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 冗余 owner 列：插入时写 experts.owner_id，之后不可改——Task 6 直接 RLS 用（不设 FK）
    owner_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")


class Skill(TimestampMixin, Base):
    __tablename__ = "skills"
    __table_args__ = (check_enum("skills", "status", ENTITY_STATUSES),)
    id: Mapped[_uuid.UUID] = pk_uuid()
    owner_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 与 experts 同构的第二个循环 FK（Uuid 类型由注解推断）
    published_revision_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("skill_revisions.id", ondelete="SET NULL", use_alter=True)
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")


class SkillRevision(TimestampMixin, Base):
    __tablename__ = "skill_revisions"
    __table_args__ = (
        UniqueConstraint("skill_id", "revision_no", name="skill_revision_no"),
        check_enum("skill_revisions", "status", REVISION_STATUSES),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    skill_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")


class ToolCatalog(Base):
    __tablename__ = "tool_catalog"
    __table_args__ = (UniqueConstraint("tool_id", "version", name="uq_tool_catalog_id_version"),)
    id: Mapped[_uuid.UUID] = pk_uuid()
    tool_id: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    permissions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RevisionTool(Base):
    __tablename__ = "revision_tools"
    __table_args__ = (
        UniqueConstraint("expert_revision_id", "tool_id", "version", name="uq_revision_tools_row"),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    expert_revision_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("expert_revisions.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)


class ContentReview(TimestampMixin, Base):
    __tablename__ = "content_reviews"
    __table_args__ = (
        check_enum("content_reviews", "target_type", ("expert_revision", "skill_revision")),
        Index("ix_content_reviews_target_hash", "target_revision_id", "content_sha256"),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    target_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # 多态引用 expert_revisions/skill_revisions，不设硬 FK
    target_revision_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(30), nullable=False)
    reviewer_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        check_enum("reports", "target_type", ("expert_revision", "skill_revision", "message")),
        check_enum("reports", "status", REPORT_STATUSES),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    reporter_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[_uuid.UUID] = mapped_column(Uuid, nullable=False)
    target_revision_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[_uuid.UUID] = pk_uuid()
    actor_id: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[_uuid.UUID | None] = mapped_column(Uuid)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
