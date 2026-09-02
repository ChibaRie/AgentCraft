from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base


class MCPTool(Base):
    __tablename__ = "mcp_tools"
    __table_args__ = (
        Index("idx_mcp_tools_server_id", "server_id"),
        CheckConstraint(
            "sensitive = 0 OR enabled = 0 OR authorized_at IS NOT NULL",
            name="ck_mcp_tools_authorization",
        ),
        UniqueConstraint("server_id", "name", name="uq_mcp_tools_server_name"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    server_id: Mapped[int] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[str] = mapped_column(Text, nullable=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    server = relationship("MCPServer", back_populates="tools")
