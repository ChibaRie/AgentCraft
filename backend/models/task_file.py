from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import Base


class TaskFile(Base):
    __tablename__ = "task_files"
    __table_args__ = (
        Index("idx_task_files_task_created", "task_id", "created_at"),
        CheckConstraint(
            "length(original_name) BETWEEN 1 AND 255", name="ck_task_files_original_name"
        ),
        CheckConstraint("length(stored_name) BETWEEN 1 AND 255", name="ck_task_files_stored_name"),
        CheckConstraint(
            "relative_path LIKE 'task-%/%' AND instr(relative_path, '..') = 0",
            name="ck_task_files_relative_path",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_task_files_size"),
        CheckConstraint(
            "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_task_files_sha256",
        ),
        UniqueConstraint("task_id", "stored_name", name="uq_task_files_stored_name"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    task = relationship("Task", back_populates="files")
