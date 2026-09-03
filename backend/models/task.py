from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_user_id", "user_id"),
        Index("idx_tasks_expert_id", "expert_id"),
        CheckConstraint(
            "status IN ('created','running','completed','failed')", name="ck_tasks_status"
        ),
        CheckConstraint(
            "workdir = '/workspaces/authorized' OR workdir LIKE '/workspaces/authorized/%'",
            name="ck_tasks_workdir",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expert_id: Mapped[int] = mapped_column(
        ForeignKey("experts.id", ondelete="RESTRICT"), nullable=False
    )
    expert_name_snapshot: Mapped[str] = mapped_column(String(50), nullable=False)
    expert_avatar_snapshot: Mapped[str | None] = mapped_column(String(500), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="created", nullable=False)
    skill_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    mcp_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    provider_config_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_providers.id", ondelete="SET NULL"), nullable=True
    )
    provider_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    workdir: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    user = relationship("User", back_populates="tasks")
    provider_config = relationship("UserProvider")
    expert = relationship("Expert", back_populates="tasks")
    conversation = relationship(
        "Conversation", back_populates="task", uselist=False, cascade="all, delete-orphan"
    )
    files = relationship("TaskFile", back_populates="task", cascade="all, delete-orphan")
