from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('user', 'expert')", name="ck_users_role"),)
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    # 与子模型 relationship 的 back_populates 对应；缺失会导致 mapper 配置失败
    experts: Mapped[list["Expert"]] = relationship(back_populates="owner")  # noqa: F821
    skills: Mapped[list["Skill"]] = relationship(back_populates="owner")  # noqa: F821
    mcp_servers: Mapped[list["MCPServer"]] = relationship(back_populates="owner")  # noqa: F821
    tasks: Mapped[list["Task"]] = relationship(back_populates="user")  # noqa: F821
    providers: Mapped[list["UserProvider"]] = relationship(
        back_populates="user"
    )  # noqa: F821
