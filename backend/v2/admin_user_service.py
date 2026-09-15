"""admin 用户管理服务（Phase 7 T3a；Sup §6:135-136/139-140）：列表/详情/quotas/entitlements。

调用契约（调用方必读）：``admin_db`` 为调用方已 begin 的 admin 会话
（端点壳 ``admin_factory().begin()`` 建，同 admin_invitation_service 形态）；
写路径业务变更与 AuditLog **同事务**收口（Sup §6:126 审计失败即回滚）。

纪律与裁决申报：

- **detail 不含任何 Key 材料**（Sup:136）：响应只读 users/user_quotas/
  user_quota_usage/tasks 四表，不触碰 user_providers/user_provider_keys（测试
  以 provider 密文金丝雀钉泄漏回归）；
- 配额默认值单一事实源 = models/catalog.py::UserQuota ORM 列默认（5/3/1/1GiB，
  task_service._ensure_quota_rows 同口径）——无行时经 `_column_defaults` 自模型
  元数据直取列默认（瞬态实例不落地 Python 列默认），不复制字面量；upsert
  **全部四维列显式赋值**（server_default 刻意省略的镜像纪律），惰性物化即建行
  （T3 Phase 6 范式同构）；
- 审计 action：user.quotas.update / user.entitlement.grant / user.entitlement.revoke
  （计划 D5 注册表）；quotas detail 含 before/after 全四维快照；
- one_active 冲突 → 409：errors.py 白名单外不得新增注册码（Phase 7 共享文件
  白名单），沿用 V1 约定形状字面（FORBIDDEN/VALIDATION_ERROR 同款先例）——
  字面 code "ENTITLEMENT_ACTIVE"；预检 + flush IntegrityError 兜底双保险；
- revoke 语义：**硬删**活跃 entitlement 行（brief 钉死「DELETE 活跃行」），
  0 行（无行或已撤销）统一 404；
- email 前缀过滤：ILIKE 'prefix%' 前缀语义，``% _ \\`` 三字符转义（用户输入
  不携带通配语义）；大小写不敏感（库内 email 全小写入库，invitation_service
  normalize_email 同口径）；
- 列表序：created_at DESC + id DESC 稳定序（admin_invitation_service.list_
  invitations 同构）。
"""

import uuid as _uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.v2.author_service import _reject_invalid_uuid as _parse_uuid
from backend.v2.ids import uuid7
from backend.v2.models import AuditLog, Task, User, UserEntitlement, UserQuota, UserQuotaUsage
from backend.v2.models.identity import ENTITLEMENTS, USER_STATUSES

_LIST_PAGE_MAX = 100
_QUOTA_DIMS = (
    "max_daily_tasks",
    "max_active_tasks",
    "max_running_tasks",
    "max_retained_storage_bytes",
)
_USAGE_DIMS = ("active_tasks", "running_tasks", "retained_storage_bytes")


def _column_defaults(model_cls: type, dims: tuple[str, ...]) -> dict:
    """读模型列默认（models/catalog.py 单一事实源——ORM Python default 即平台默认）。

    瞬态实例不落地列默认（默认在 flush 编译期才应用），故自元数据直取 ColumnDefault.arg。
    """
    return {name: model_cls.__table__.c[name].default.arg for name in dims}


def _validation(message: str) -> HTTPException:
    # VALIDATION_ERROR 为 V1 约定形状字面（不经 ErrorCode 注册表，同 admin_invitation_service）
    return HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": message})


def _unified_404() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "用户不存在"})


def _entitlement_active() -> HTTPException:
    # 409 约定形状字面：one_active 部分唯一索引冲突（错误注册表白名单外不新增）
    return HTTPException(
        status_code=409,
        detail={"code": "ENTITLEMENT_ACTIVE", "message": "该用户已持有此活跃 entitlement"},
    )


def _validate_kind(kind: str) -> str:
    if kind not in ENTITLEMENTS:
        raise _validation(f"entitlement kind 仅支持 {'/'.join(ENTITLEMENTS)}")
    return kind


async def _require_user(admin_db: AsyncSession, user_id: str) -> _uuid.UUID:
    uid = _parse_uuid(user_id, "user_id")
    exists = (await admin_db.execute(select(User.id).where(User.id == uid))).scalar_one_or_none()
    if exists is None:
        raise _unified_404()
    return uid


# ---------------------------------------------------------------------------
# 列表（元数据读免 reason）
# ---------------------------------------------------------------------------


async def list_users(
    admin_db: AsyncSession,
    *,
    email_prefix: str | None,
    status: str | None,
    page: int,
    size: int,
) -> dict:
    """用户列表（email 前缀/状态过滤 + 分页；Sup:135）。返回 {items,total,page,size}。"""
    if page < 1 or size < 1 or size > _LIST_PAGE_MAX:
        raise _validation("分页参数非法（page≥1，1≤size≤100）")
    if status is not None and status not in USER_STATUSES:
        raise _validation("status 词表非法（pending/active/suspended/deleting/deleted）")
    conds = []
    if email_prefix is not None:
        # 前缀语义 + 通配字符转义（\_ % _），ILIKE 大小写不敏感（库内全小写）
        escaped = email_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append(User.email.ilike(f"{escaped}%", escape="\\"))
    if status is not None:
        conds.append(User.status == status)
    total = int(
        (await admin_db.execute(select(func.count()).select_from(User).where(*conds))).scalar_one()
    )
    rows = (
        (
            await admin_db.execute(
                select(User)
                .where(*conds)
                .order_by(User.created_at.desc(), User.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    items = [
        {
            "id": str(u.id),
            "email": u.email,
            "role": u.role,
            "status": u.status,
            "created_at": u.created_at.isoformat(),
        }
        for u in rows
    ]
    return {"items": items, "total": total, "page": page, "size": size}


# ---------------------------------------------------------------------------
# 详情（配额/用量/任务计数；不含任何 Key 材料——Sup:136）
# ---------------------------------------------------------------------------


async def get_user_detail(admin_db: AsyncSession, *, user_id: str) -> dict:
    """用户详情：user 元数据 + 配额（无行取 ORM 列默认）+ 用量 + 任务计数。"""
    uid = _parse_uuid(user_id, "user_id")
    user = (await admin_db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if user is None:
        raise _unified_404()

    quota_row = (
        await admin_db.execute(select(UserQuota).where(UserQuota.user_id == uid))
    ).scalar_one_or_none()
    quotas = (
        {dim: int(getattr(quota_row, dim)) for dim in _QUOTA_DIMS}
        if quota_row is not None
        else _column_defaults(UserQuota, _QUOTA_DIMS)  # 无行：平台默认（单一事实源）
    )
    usage_row = (
        await admin_db.execute(select(UserQuotaUsage).where(UserQuotaUsage.user_id == uid))
    ).scalar_one_or_none()
    usage = (
        {dim: int(getattr(usage_row, dim)) for dim in _USAGE_DIMS}
        if usage_row is not None
        else _column_defaults(UserQuotaUsage, _USAGE_DIMS)
    )

    by_status = {
        status: int(n)
        for status, n in (
            await admin_db.execute(
                select(Task.status, func.count()).where(Task.owner_id == uid).group_by(Task.status)
            )
        ).all()
    }
    return {
        "user": {
            "id": str(user.id),
            "email": user.email,
            "role": user.role,
            "status": user.status,
            "created_at": user.created_at.isoformat(),
        },
        "quotas": quotas,
        "usage": usage,
        "tasks": {"total": sum(by_status.values()), "by_status": by_status},
    }


# ---------------------------------------------------------------------------
# 配额调整（user_quotas 无 RLS，admin blanket ALL 0001:1025 直写；0009 零动作）
# ---------------------------------------------------------------------------


def _validate_quotas(quotas: dict) -> dict:
    """四维白名单 + 非负整数门：未知键/空体/bool/非 int/负值 → 400 VALIDATION_ERROR。"""
    unknown = set(quotas) - set(_QUOTA_DIMS)
    if unknown:
        raise _validation(f"未知配额维度：{sorted(unknown)}")
    if not quotas:
        raise _validation("至少需要一个配额维度字段")
    for key, value in quotas.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _validation(f"{key} 须为非负整数")
    return quotas


async def update_quotas(
    admin_db: AsyncSession,
    *,
    admin_id: str,
    user_id: str,
    quotas: dict,
    reason: str,
    request_id: str | None,
) -> dict:
    """四维配额 upsert（字段可选；无行惰性物化）+ 审计 user.quotas.update（before/after）。

    before/after 为全四维快照（before 无行时取 ORM 列默认）；upsert 全列显式赋值
    （server_default 刻意省略的镜像纪律）；用户不存在 → 统一 404。
    """
    _validate_quotas(quotas)
    uid = await _require_user(admin_db, user_id)
    existing = (
        await admin_db.execute(select(UserQuota).where(UserQuota.user_id == uid).with_for_update())
    ).scalar_one_or_none()
    if existing is not None:
        before = {dim: int(getattr(existing, dim)) for dim in _QUOTA_DIMS}
    else:
        before = _column_defaults(UserQuota, _QUOTA_DIMS)  # 无行：平台默认（单一事实源）
    after = {**before, **quotas}
    await admin_db.execute(
        pg_insert(UserQuota)
        .values(user_id=uid, **after)
        .on_conflict_do_update(index_elements=["user_id"], set_=after)
    )
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action="user.quotas.update",
            target_type="user",
            target_id=uid,
            reason=reason,
            request_id=request_id,
            detail={"user_id": str(uid), "before": before, "after": after},
        )
    )
    await admin_db.flush()
    return {"user_id": str(uid), "quotas": after}


# ---------------------------------------------------------------------------
# entitlement 授予/撤销（0009 admin INSERT/DELETE policy 承载）
# ---------------------------------------------------------------------------


async def grant_entitlement(
    admin_db: AsyncSession,
    *,
    admin_id: str,
    user_id: str,
    kind: str,
    reason: str,
    request_id: str | None,
) -> dict:
    """授予 entitlement（one_active 部分唯一索引：活跃行已存在 → 409 字面
    ENTITLEMENT_ACTIVE，预检 + flush IntegrityError 双保险）+ 审计 grant。"""
    _validate_kind(kind)
    uid = await _require_user(admin_db, user_id)
    active = (
        await admin_db.execute(
            select(UserEntitlement.id).where(
                UserEntitlement.user_id == uid,
                UserEntitlement.entitlement == kind,
                UserEntitlement.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if active is not None:  # one_active_entitlement 谓词同款预检
        raise _entitlement_active()
    ent_id = uuid7()
    admin_db.add(
        UserEntitlement(id=ent_id, user_id=uid, entitlement=kind, granted_by=_uuid.UUID(admin_id))
    )
    try:
        await admin_db.flush()
    except IntegrityError as exc:  # 并发同 (user, kind) 撞部分唯一索引（预检后竞态窗口）
        raise _entitlement_active() from exc
    # granted_at server_default 由 DB 落值，flush 后回读（server_default 非列默认，
    # refresh 之外用显式 SELECT 取——同一会话事务内读自己已 flush 的行合法）
    granted_at = (
        await admin_db.execute(
            select(UserEntitlement.granted_at).where(UserEntitlement.id == ent_id)
        )
    ).scalar_one()
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action="user.entitlement.grant",
            target_type="user",
            target_id=uid,
            reason=reason,
            request_id=request_id,
            detail={
                "user_id": str(uid),
                "entitlement": kind,
                "entitlement_id": str(ent_id),
            },
        )
    )
    await admin_db.flush()
    return {
        "user_id": str(uid),
        "entitlement": kind,
        "entitlement_id": str(ent_id),
        "granted_at": granted_at.isoformat(),
    }


async def revoke_entitlement(
    admin_db: AsyncSession,
    *,
    admin_id: str,
    user_id: str,
    kind: str,
    reason: str,
    request_id: str | None,
) -> dict:
    """撤销 entitlement：硬删活跃行（brief「DELETE 活跃行」）；0 行统一 404；
    审计 user.entitlement.revoke（detail 记被删行 id）。"""
    _validate_kind(kind)
    uid = await _require_user(admin_db, user_id)
    row = (
        await admin_db.execute(
            select(UserEntitlement)
            .where(
                UserEntitlement.user_id == uid,
                UserEntitlement.entitlement == kind,
                UserEntitlement.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:  # 无行 / 已撤销同形 404（统一 404 纪律）
        raise _unified_404()
    ent_id = row.id
    await admin_db.execute(delete(UserEntitlement).where(UserEntitlement.id == ent_id))
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action="user.entitlement.revoke",
            target_type="user",
            target_id=uid,
            reason=reason,
            request_id=request_id,
            detail={
                "user_id": str(uid),
                "entitlement": kind,
                "entitlement_id": str(ent_id),
            },
        )
    )
    await admin_db.flush()
    return {
        "user_id": str(uid),
        "entitlement": kind,
        "entitlement_id": str(ent_id),
        "revoked_at": datetime.now(timezone.utc).isoformat(),
    }
