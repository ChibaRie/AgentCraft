from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base


class UserProvider(Base):
    """用户自带 Provider 配置（BYOK，DB 设计 §3.12）。

    api_key_encrypted 为 AES-256-GCM 信封 JSON（手册 §11.3 同款方案，
    AAD=agentcraft:user_providers:{user_id}:api_key:v1）；接口永不回传
    明文/密文，仅返回尾 4 位掩码提示。faux 为内置测试项不入本表。
    """

    __tablename__ = "user_providers"
    __table_args__ = (
        Index("idx_user_providers_user_id", "user_id"),
        UniqueConstraint("user_id", "name", name="uq_user_providers_user_name"),
        CheckConstraint("protocol IN ('openai')", name="ck_user_providers_protocol"),
        CheckConstraint("is_default IN (0,1)", name="ck_user_providers_is_default"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(30), nullable=False)
    protocol: Mapped[str] = mapped_column(String(20), default="openai", nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_hint: Mapped[str | None] = mapped_column(String(8), nullable=True)  # 尾 4 位掩码，写时派生
    model_id: Mapped[str] = mapped_column(String(100), nullable=False)
    is_default: Mapped[int] = mapped_column(default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    user = relationship("User", back_populates="providers")
