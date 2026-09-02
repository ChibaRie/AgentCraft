from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base


class Expert(Base):
    __tablename__ = "experts"
    __table_args__ = (
        Index("idx_experts_owner_id", "owner_id"),
        Index("idx_experts_status_category", "status", "category"),
        CheckConstraint(
            "category IN ('tech','design','writing','data_analysis','office','other')",
            name="ck_experts_category",
        ),
        CheckConstraint("status IN ('draft','published','offline')", name="ck_experts_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(100), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    methodology: Mapped[str] = mapped_column(Text, nullable=False)
    task_examples: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    owner = relationship("User", back_populates="experts")
    skills = relationship("ExpertSkill", back_populates="expert", cascade="all, delete-orphan")
    mcps = relationship("ExpertMCP", back_populates="expert", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="expert")
