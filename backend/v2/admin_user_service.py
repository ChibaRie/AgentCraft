"""admin 用户管理服务（Phase 7 T3a/T3b；Sup §6:135-136/139-140/137-138）。

T3a：列表/详情/quotas/entitlements。T3b：suspend/unsuspend + 级联两段拆分
（D3：原子部分 admin 事务内、级联部分壳层 post-commit；T5 ban_author 复用
`suspend_user_atomic`/`run_suspension_cascade` 原语）。

调用契约（调用方必读）：``admin_db`` 为调用方已 begin 的 admin 会话
（端点壳 ``admin_factory().begin()`` 建，同 admin_invitation_service 形态）；
写路径业务变更与 AuditLog **同事务**收口（Sup §6:126 审计失败即回滚）。
`run_suspension_cascade` 是**壳层 post-commit 专用**（不在 admin 事务内调）——
内部自持 owner_session/admin 只读会话（D16 两段式）。

纪律与裁决申报：

- **detail 不含任何 Key 材料**（Sup:136）：响应只读 users/user_quotas/
  user_quota_usage/tasks 四表，不触碰 user_providers/user_provider_keys（测试
  以 provider 密文金丝雀钉泄漏回归）；
- 配额默认值单一事实源 = models/catalog.py::UserQuota ORM 列默认（5/3/1/1GiB，
  task_service._ensure_quota_rows 同口径）——无行时经 `_column_defaults` 自模型
  元数据直取列默认（瞬态实例不落地 Python 列默认），不复制字面量；upsert
  **全部四维列显式赋值**（server_default 刻意省略的镜像纪律），惰性物化即建行
  （T3 Phase 6 范式同构）；
- 审计 action：user.quotas.update / user.entitlement.grant / user.entitlement.revoke /
  user.suspend / user.unsuspend（计划 D5 注册表）；quotas detail 含 before/after
  全四维快照；suspend 原子部分**不做审计**（调用方按 action 写：user.suspend /
  report.ban_author）；
- one_active 冲突 → 409：errors.py 白名单外不得新增注册码（Phase 7 共享文件
  白名单），沿用 V1 约定形状字面（FORBIDDEN/VALIDATION_ERROR 同款先例）——
  字面 code "ENTITLEMENT_ACTIVE"；预检 + flush IntegrityError 兜底双保险；
- revoke 语义：**硬删**活跃 entitlement 行（brief 钉死「DELETE 活跃行」），
  0 行（无行或已撤销）统一 404；
- suspend 状态冲突 → 409：同款字面 code "USER_STATUS_CONFLICT"（C4 原子谓词
  rowcount=0 时存在行但状态非 active|deleting：pending/suspended/deleted）；
- email 前缀过滤：ILIKE 'prefix%' 前缀语义，``% _ \\`` 三字符转义（用户输入
  不携带通配语义）；大小写不敏感（库内 email 全小写入库，invitation_service
  normalize_email 同口径）；
- 列表序：created_at DESC + id DESC 稳定序（admin_invitation_service.list_
  invitations 同构）。
"""

import uuid as _uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.v2.author_service import _reject_invalid_uuid as _parse_uuid
from backend.v2.ids import uuid7
from backend.v2.models import AuditLog, Task, User, UserEntitlement, UserQuota, UserQuotaUsage
from backend.v2.models.identity import ENTITLEMENTS, USER_STATUSES
from backend.v2.runtime import V2Runtime, owner_session
from backend.v2.session_service import revoke_all
from backend.v2.task_release import release_task_holdings
from backend.v2.task_service import _add_event, _allocate_event_sequence

_LIST_PAGE_MAX = 100
_QUOTA_DIMS = (
    "max_daily_tasks",
    "max_active_tasks",
    "max_running_tasks",
    "max_retained_storage_bytes",
)
_USAGE_DIMS = ("active_tasks", "running_tasks", "retained_storage_bytes")

# 级联 stop_round 的 bounded-stop 上限（D3/D4 钉 5s；与 _ROUND_DEADLINE_STOP_TIMEOUT 同值）
_SUSPEND_STOP_TIMEOUT: float = 5.0


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


def _user_status_conflict(message: str) -> HTTPException:
    # 409 约定形状字面：用户状态不满足操作前置（同 ENTITLEMENT_ACTIVE 先例，
    # 错误注册表白名单外不新增注册码）
    return HTTPException(
        status_code=409, detail={"code": "USER_STATUS_CONFLICT", "message": message}
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


# ---------------------------------------------------------------------------
# suspend/unsuspend + 级联两段拆分（Phase 7 T3b，D3/D16/C3/C4）
# ---------------------------------------------------------------------------

# C4 单语句翻转（D3）：active|deleting→suspended；deleting 行同语句清 deadline
# （deletion_deadline_consistency CHECK：deadline 非空仅允许 status='deleting'，
# 二段式 UPDATE 会撞 CHECK——单语句 CASE 一次收口）。行值谓词即仲裁。
_SUSPEND_USER_SQL = text(
    "UPDATE users SET status = 'suspended', "
    "deletion_deadline_at = CASE WHEN status = 'deleting' THEN NULL ELSE deletion_deadline_at END "
    "WHERE id = :u AND status IN ('active','deleting')"
)
_UNSUSPEND_USER_SQL = text(
    "UPDATE users SET status = 'active' WHERE id = :u AND status = 'suspended'"
)

# 级联①（C3 零新 policy）：deletion_cancel 令牌条件置废（owner_session GUC=目标
# 用户合法写；封禁优先于撤销期，Sup:167）
_SUSPEND_INVALIDATE_TOKENS_SQL = text(
    "UPDATE account_action_tokens SET consumed_at = now() "
    "WHERE user_id = :u AND purpose = 'deletion_cancel' AND consumed_at IS NULL"
)

# 级联②圈定（D16 阶段 1）：admin 只读会话 plain SELECT **无 FOR UPDATE**（RLS 表
# 锁定读需 SELECT+UPDATE 双 policy，admin 只有 SELECT，带锁即静默 0 行——
# task_dispatcher 范式）
_SUSPEND_TASK_CANDIDATES_SQL = text(
    "SELECT id, owner_id, status FROM tasks "
    "WHERE owner_id = :u AND status IN ('queued','running') ORDER BY created_at, id"
)
_SUSPEND_ACTIVE_ROUND_SQL = text(
    "SELECT id FROM task_rounds WHERE task_id = :tid AND state = 'running' "
    "ORDER BY created_at DESC LIMIT 1"
)

# 级联②翻转（D16 阶段 2，owner_session 内；对齐 task_executor.py:177-186
# _TERMINATOR_* 形态——对齐新写申报：flip 的 abort_reason 不同（admin_suspended），
# read/cancel 语义相同但不跨模块引入 executor 模块私有面）
_SUSPEND_TASK_READ_SQL = text("SELECT status FROM tasks WHERE id = :tid FOR UPDATE")
_SUSPEND_TASK_FLIP_SQL = text(
    "UPDATE tasks SET status = 'aborted', abort_reason = 'admin_suspended', "
    "pending_terminal = NULL "
    "WHERE id = :tid AND status IN ('queued','running')"
)
_SUSPEND_ROUND_CANCEL_SQL = text(
    "UPDATE task_rounds SET state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL "
    "WHERE task_id = :tid AND state IN ('pending','running','cancelling') "
    "RETURNING id"
)


async def suspend_user_atomic(
    admin_db: AsyncSession, *, target_user_id: str, revoke_entitlement: bool
) -> dict:
    """suspend 原子部分（admin_db 已 begin 事务内，D3）。

    C4 单语句翻转（active|deleting→suspended + deleting 行同语句清 deadline，
    行值谓词仲裁）+ [revoke_entitlement] 撤 expert_author 活跃行（T3a
    revoke_entitlement 的锁行+硬删形态；**0 行容忍**——无 entitlement 的作者
    也可被 ban，D3 裁决）。rowcount=0 以回读区分 404（无此行）/409（状态非
    active|deleting）。**不做审计**（调用方按 action 写：user.suspend /
    report.ban_author）。

    返回 {user_id, before_status, status: 'suspended', deadline_cleared[, entitlement_revoked]}；
    entitlement_revoked 仅 revoke_entitlement=True 时出现（撤销的活跃行数>0）。
    """
    uid = _parse_uuid(target_user_id, "user_id")
    before = (
        await admin_db.execute(select(User.status).where(User.id == uid))
    ).scalar_one_or_none()
    if before is None:
        raise _unified_404()
    flipped = await admin_db.execute(_SUSPEND_USER_SQL, {"u": uid})
    if flipped.rowcount == 0:
        # 行在首读后消失（并发物理场景不存在——users 行不物理删）或状态竞变：
        # 回读区分 404/409（belt-and-braces）
        fresh = (
            await admin_db.execute(select(User.status).where(User.id == uid))
        ).scalar_one_or_none()
        if fresh is None:
            raise _unified_404()
        raise _user_status_conflict("仅 active/deleting 用户可被停用")
    result: dict = {
        "user_id": str(uid),
        "before_status": before,
        "status": "suspended",
        "deadline_cleared": before == "deleting",
    }
    if revoke_entitlement:
        row = (
            await admin_db.execute(
                select(UserEntitlement.id)
                .where(
                    UserEntitlement.user_id == uid,
                    UserEntitlement.entitlement == "expert_author",
                    UserEntitlement.revoked_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is not None:  # 0 行容忍（无 entitlement 的作者也可被 ban——D3）
            await admin_db.execute(delete(UserEntitlement).where(UserEntitlement.id == row))
            result["entitlement_revoked"] = True
    return result


async def _suspend_one_task(db: AsyncSession, *, task_id: str, owner_id: str) -> dict:
    """级联②单任务事务体（owner_session 已设 GUC；对齐 build_terminator 的
    _terminate_one 形态）：fresh 状态锁读分支 → 活跃轮条件收口 + 任务
    aborted(admin_suspended)（终结类条件 UPDATE，同态=幂等成功不抛）+
    status_changed/round_cancelled 事件 + 终态档释放。"""
    fresh = (await db.execute(_SUSPEND_TASK_READ_SQL, {"tid": task_id})).scalar_one_or_none()
    if fresh not in ("queued", "running"):
        # 已终态/已翻转/行已消失（D18 物理删）——同态幂等，分文不写
        return {"flipped": False, "round_cancelled": []}
    cancelled_rounds = list(
        (await db.execute(_SUSPEND_ROUND_CANCEL_SQL, {"tid": task_id})).scalars()
    )
    flipped = await db.execute(_SUSPEND_TASK_FLIP_SQL, {"tid": task_id})
    if flipped.rowcount == 0:  # 行锁在握，理论不可达——条件仲裁双保险
        return {"flipped": False, "round_cancelled": []}
    step = 1 + len(cancelled_rounds)
    seq = await _allocate_event_sequence(db, _uuid.UUID(task_id), step)
    _add_event(
        db,
        task_id=_uuid.UUID(task_id),
        owner_id=_uuid.UUID(owner_id),
        sequence=seq,
        event_type="status_changed",
        payload={"status": "aborted", "reason": "admin_suspended"},
    )
    for offset, round_id in enumerate(cancelled_rounds, start=1):
        _add_event(
            db,
            task_id=_uuid.UUID(task_id),
            owner_id=_uuid.UUID(owner_id),
            sequence=seq + offset,
            event_type="round_cancelled",
            payload={"round_id": str(round_id), "reason": "admin_suspended"},
            round_id=round_id,
        )
    await db.flush()
    await release_task_holdings(db, task_id=task_id, owner_id=owner_id)
    return {"flipped": True, "round_cancelled": [str(r) for r in cancelled_rounds]}


async def run_suspension_cascade(runtime: V2Runtime, *, target_user_id: str, executor) -> dict:
    """suspend 级联（壳层 post-commit 专用，D3——不在 admin 事务内调）。

    ①owner_session（GUC=目标用户，C3 零新 policy）单事务：revoke_all（软撤销
    全部会话）+ deletion_cancel 令牌条件置废；②两段式任务回收（D16）：admin
    只读 plain SELECT 圈定该用户 queued/running 任务 → running 任务先
    executor.stop_round（bounded 5s；executor None 降级=状态照翻、轮由 reclaim
    fence 兜底）→ 逐任务 owner_session 单事务条件翻转 aborted(admin_suspended)
    + pending/活跃轮 cancelled + release_task_holdings。

    各段条件 UPDATE/谓词天然幂等——重试自愈（D3 已知代价：post-commit 失败=
    状态已翻转但级联未竟的有界窗口，重跑本函数即收敛）。异常向调用方传播
    （与 build_terminator 同款——壳层 logger.exception 后照常返回任务态）。
    返回 {sessions_revoked, tokens_invalidated, flipped_tasks, cancelled_rounds, stopped}。
    """
    uid = _parse_uuid(target_user_id, "user_id")
    receipts: dict = {
        "sessions_revoked": 0,
        "tokens_invalidated": 0,
        "flipped_tasks": 0,
        "cancelled_rounds": 0,
        "stopped": 0,
    }
    async with owner_session(runtime, str(uid)) as db:
        receipts["sessions_revoked"] = await revoke_all(db, uid)
        receipts["tokens_invalidated"] = (
            await db.execute(_SUSPEND_INVALIDATE_TOKENS_SQL, {"u": uid})
        ).rowcount
    async with runtime.admin_factory() as session:
        candidates = (await session.execute(_SUSPEND_TASK_CANDIDATES_SQL, {"u": uid})).all()
    for task_id, owner_id, status in candidates:
        task_id = str(task_id)
        if status == "running" and executor is not None:
            async with runtime.admin_factory() as session:
                row = (await session.execute(_SUSPEND_ACTIVE_ROUND_SQL, {"tid": task_id})).first()
            if row is not None:
                stop = await executor.stop_round(
                    str(row.id), reason="admin_suspended", timeout=_SUSPEND_STOP_TIMEOUT
                )
                if stop["stopped"]:
                    receipts["stopped"] += 1
        async with owner_session(runtime, str(owner_id)) as db:
            receipt = await _suspend_one_task(db, task_id=task_id, owner_id=str(owner_id))
        if receipt["flipped"]:
            receipts["flipped_tasks"] += 1
        receipts["cancelled_rounds"] += len(receipt["round_cancelled"])
    return receipts


async def suspend_user(
    admin_db: AsyncSession,
    runtime: V2Runtime,
    *,
    admin_id: str,
    user_id: str,
    reason: str,
    request_id: str | None,
    executor,
) -> dict:
    """admin 停用用户（admin_db 已 begin 事务内）：suspend_user_atomic
    (revoke_entitlement=False) + 审计 user.suspend（detail 含 before_status 与
    deadline 清除标记）。**不做级联**——级联由壳层提交后调
    run_suspension_cascade（kill_tool terminator 同款序，D3）。

    runtime/executor 为计划冻结签名保留位（D1 服务签名形态；级联原语由壳层
    直接消费，本函数零依赖）。"""
    result = await suspend_user_atomic(admin_db, target_user_id=user_id, revoke_entitlement=False)
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action="user.suspend",
            target_type="user",
            target_id=_uuid.UUID(user_id),
            reason=reason,
            request_id=request_id,
            detail={
                "user_id": result["user_id"],
                "before_status": result["before_status"],
                "deadline_cleared": result["deadline_cleared"],
            },
        )
    )
    await admin_db.flush()
    return result


async def unsuspend_user(
    admin_db: AsyncSession,
    *,
    admin_id: str,
    user_id: str,
    reason: str,
    request_id: str | None,
) -> dict:
    """admin 恢复用户（admin_db 已 begin 事务内）：suspended→active 条件 UPDATE
    （无 deadline 冲突——suspend 已清列；rowcount=0 回读区分 404/409）+ 审计
    user.unsuspend。无级联段（纯 admin 事务）。"""
    uid = _parse_uuid(user_id, "user_id")
    before = (
        await admin_db.execute(select(User.status).where(User.id == uid))
    ).scalar_one_or_none()
    if before is None:
        raise _unified_404()
    flipped = await admin_db.execute(_UNSUSPEND_USER_SQL, {"u": uid})
    if flipped.rowcount == 0:
        fresh = (
            await admin_db.execute(select(User.status).where(User.id == uid))
        ).scalar_one_or_none()
        if fresh is None:
            raise _unified_404()
        raise _user_status_conflict("仅 suspended 用户可被恢复")
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action="user.unsuspend",
            target_type="user",
            target_id=uid,
            reason=reason,
            request_id=request_id,
            detail={"user_id": str(uid), "before_status": before},
        )
    )
    await admin_db.flush()
    return {"user_id": str(uid), "before_status": before, "status": "active"}
