"""身份与访问模型（6 张表）— 对齐 AgentCraft Database Design v2.0.0 §2-3。

users / user_entitlements / invitations / account_action_tokens / email_outbox / sessions。
枚举值常量（USER_STATUSES 等）供阶段 2 认证域直接消费。
部分唯一索引 one_active_entitlement / invitations_one_open_email 与 Task 6 迁移 SQL
（Database Design §3）一字不差，模型侧与迁移侧双保险。
"""
import uuid as _uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.v2.models.base import Base, TimestampMixin, check_enum, pk_uuid

USER_ROLES = ("user", "admin")
USER_STATUSES = ("pending", "active", "suspended", "deleting", "deleted")
ENTITLEMENTS = ("expert_author",)
ACTION_TOKEN_PURPOSES = ("email_verify", "password_reset", "deletion_cancel")
OUTBOX_PURPOSES = ("invitation", "email_verify", "password_reset", "deletion_cancel")
OUTBOX_STATES = ("pending", "sent", "delivery_failed")


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        check_enum("users", "role", USER_ROLES),
        check_enum("users", "status", USER_STATUSES),
        CheckConstraint("deleted_at IS NULL OR status = 'deleted'", name="deleted_consistency"),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    mfa_secret_enc: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserEntitlement(Base):
    __tablename__ = "user_entitlements"
    __table_args__ = (
        check_enum("user_entitlements", "entitlement", ENTITLEMENTS),
        Index(
            "one_active_entitlement",
            "user_id",
            "entitlement",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    user_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    entitlement: Mapped[str] = mapped_column(String(40), nullable=False)
    granted_by: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invitation(TimestampMixin, Base):
    __tablename__ = "invitations"
    __table_args__ = (
        Index(
            "invitations_one_open_email",
            "email",
            unique=True,
            postgresql_where=text("consumed_at IS NULL AND revoked_at IS NULL"),
        ),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[_uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class AccountActionToken(Base):
    __tablename__ = "account_action_tokens"
    __table_args__ = (check_enum("account_action_tokens", "purpose", ACTION_TOKEN_PURPOSES),)
    id: Mapped[_uuid.UUID] = pk_uuid()
    user_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailOutbox(Base):
    __tablename__ = "email_outbox"
    __table_args__ = (
        check_enum("email_outbox", "purpose", OUTBOX_PURPOSES),
        check_enum("email_outbox", "state", OUTBOX_STATES),
    )
    id: Mapped[_uuid.UUID] = pk_uuid()
    user_id: Mapped[_uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    payload_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(TimestampMixin, Base):
    __tablename__ = "sessions"
    id: Mapped[_uuid.UUID] = pk_uuid()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[_uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
