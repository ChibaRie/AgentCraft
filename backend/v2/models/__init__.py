"""聚合出口：alembic_v2 env.py 的 target_metadata 指向 Base.metadata。
新模型必须在此 import 才会进入 autogenerate 视野。"""
from backend.v2.models import identity
from backend.v2.models.base import Base, TimestampMixin, check_enum, pk_uuid
from backend.v2.models.identity import (
    ACTION_TOKEN_PURPOSES,
    ENTITLEMENTS,
    OUTBOX_PURPOSES,
    OUTBOX_STATES,
    USER_ROLES,
    USER_STATUSES,
    AccountActionToken,
    EmailOutbox,
    Invitation,
    Session,
    User,
    UserEntitlement,
)

__all__ = [
    "ACTION_TOKEN_PURPOSES",
    "ENTITLEMENTS",
    "OUTBOX_PURPOSES",
    "OUTBOX_STATES",
    "USER_ROLES",
    "USER_STATUSES",
    "AccountActionToken",
    "Base",
    "EmailOutbox",
    "Invitation",
    "Session",
    "TimestampMixin",
    "User",
    "UserEntitlement",
    "check_enum",
    "identity",
    "pk_uuid",
]
