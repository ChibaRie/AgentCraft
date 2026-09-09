"""聚合出口：alembic_v2 env.py 的 target_metadata 指向 Base.metadata。
新模型必须在此 import 才会进入 autogenerate 视野。"""
from backend.v2.models.base import Base, TimestampMixin, check_enum, pk_uuid

__all__ = ["Base", "TimestampMixin", "check_enum", "pk_uuid"]
