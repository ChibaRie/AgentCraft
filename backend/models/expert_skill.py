from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base


class ExpertSkill(Base):
    __tablename__ = "expert_skills"
    __table_args__ = (
        Index("idx_expert_skills_skill_id", "skill_id"),
        UniqueConstraint("expert_id", "skill_id", name="uq_expert_skills_pair"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    expert_id: Mapped[int] = mapped_column(
        ForeignKey("experts.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[int] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    expert = relationship("Expert", back_populates="skills")
    skill = relationship("Skill", back_populates="experts")
