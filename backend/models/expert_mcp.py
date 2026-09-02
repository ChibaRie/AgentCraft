from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base


class ExpertMCP(Base):
    __tablename__ = "expert_mcps"
    __table_args__ = (
        Index("idx_expert_mcps_server_id", "server_id"),
        UniqueConstraint("expert_id", "server_id", name="uq_expert_mcps_pair"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    expert_id: Mapped[int] = mapped_column(
        ForeignKey("experts.id", ondelete="CASCADE"), nullable=False
    )
    server_id: Mapped[int] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    expert = relationship("Expert", back_populates="mcps")
    server = relationship("MCPServer", back_populates="experts")
