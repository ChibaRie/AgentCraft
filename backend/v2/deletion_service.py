"""账户注销（请求/撤销）+ 到期清理作业（Task 13）。

契约出处：task-13-brief + Supplement §2 + 裁决 A10/A12/A15。事务契约（调用方必读）：

- request（认证端点，get_v2_auth 已过会话/CSRF/状态门；Idempotency-Key 必带 A5，
  subject=``subject_user``）：状态门（仅 status='active' 可发起；pending → 409
  ACCOUNT_PENDING、suspended → 403 ACCOUNT_SUSPENDED（T7 文案）、deleting → 409
  ACCOUNT_DELETING「注销处理中」——HTTP 面 suspended/deleting 先被 get_v2_auth
  拦截/会话已失效（A10），状态门为服务层兜底与直调契约）→ 再认证（A10 口径：
  密码必验，失败 401 INVALID_CREDENTIALS「当前密码不正确」；TOTP 已启用叠加验码，
  A15 排序先于 Argon2，失败计入 ``password_change_totp``——与改密端点同威胁同
  scope，直接复用其校验面避免第三份解密副本）→ 幂等 begin（app 会话，先于 owner
  事务；**状态门/再认证先于 begin**——§7「重放优先」的偏离不可观测：成功请求
  revoke_all 后全会话失效，携带同 key 的重放请求无法再通过认证达及本服务，失败
  请求不写幂等记录亦无可重放）→ owner 单事务（GUC=user）：active→deleting
  跃迁（deletion_deadline_at = now()+14d，rowcount=1 复核，RETURNING 以 DB 时钟
  求剩余天数）→ ``revoke_all``（A10：含当前会话，全会话失效）→
  ``TERMINATE_TASKS_HOOK``（模块级钩子：Phase 2 默认 no-op，任务域 Phase 6 注入
  任务终止/卷清理）→ deletion_cancel 令牌（14 天，与 outbox 模板有效期同源
  ``VALID_HOURS['deletion_cancel']``=336h）+ outbox 同事务（入队异常 → 整体回滚，
  绝不进入 deleting）→ 幂等 store；成功 200 {status, days_remaining} +
  clear_session_cookie（全会话已死，双 cookie 一并清除）。
- cancel（公开端点，无会话无 CSRF；Idempotency-Key 必带 A5，subject=
  ``subject_token``）：幂等 begin 最先（重放优先于一切状态检查——同 key 命中原样
  重放原 200，无论令牌当前已消费与否）→ admin 预检按 token_hash +
  purpose='deletion_cancel' 定位（鸡蛋问题同 T10/T12）→ 未命中：限流
  ``deletion_cancel_invalid`` 主体 [HMAC(ip)] 后统一 409 → 命中：限流
  ``deletion_cancel_attempt`` 主体 [HMAC(user_id), HMAC(ip)]（合法路径与无效探测
  分桶计数：合法重试不消耗探测桶）→ owner 事务（GUC=令牌行 user_id）：令牌 FOR
  UPDATE 重校验（未消费未过期，DB 时钟）→ 用户须 status='deleting'（否则统一
  409，事务回滚分文不消费）→ 验密（失败 401，与改密端点统一文案）→ active 跃迁
  （deletion_deadline_at 置空，rowcount=1 复核）→ 消费令牌 → ``create_session``
  （A12：全新会话——不复活任何旧会话）→ 幂等 store 同事务；成功 200
  {user, csrf_token} + 种双 cookie。
- sweep_expired（admin 会话圈定 → 逐用户 owner 事务）：SELECT deleting 且
  deadline <= now() → 逐用户匿名化 UPDATE（status='deleted'、deleted_at=now()、
  deadline 置空、email='deleted+'||id||'@users.invalid'——id 内嵌满足唯一约束，
  邮箱地址不可回收；mfa_secret_enc 清空；password_hash 换随机 Argon2（事务外
  预算，昂贵 CPU 不持行锁）——凭据全灭）→ ``revoke_all``（rowcount=1 才执行——
  并发 cancel 恢复的用户不得误杀其新会话）。users 行不删除：审计等 FK 引用
  （SET NULL 语义）经行存活而保留。Key 密文删除属 user_providers 域（DB Design
  §157；裁决 D15）——Phase 3 已交付：匿名化后同事务显式 DELETE user_providers
  行（users 行不删除 → user_id CASCADE 永不触发，必须显式删；tasks.provider_id
  FK RESTRICT，Phase 6 接线 TERMINATE_TASKS_HOOK 时任务清理须先于此 DELETE）。
  任务卷清理仍属任务域 Phase 6（经 TERMINATE_TASKS_HOOK 注入）。

防探测（cancel）：全部失效形态（未知/错 purpose/过期/已消费/非 deleting 状态）
统一 409 ACCOUNT_DELETING「撤销链接无效或已过期」——同码同文案同状态，响应体
逐字节一致。失败路径零副作用：无令牌消费、无幂等记录（begin 未命中即零写入）。
"""

import asyncio
import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.idempotency import begin, store, subject_token, subject_user
from backend.v2.models import AccountActionToken
from backend.v2.outbox import VALID_HOURS, enqueue
from backend.v2.password_service import _verify_change_totp
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, owner_session
from backend.v2.security import generate_token, hash_password, hash_token, verify_password
from backend.v2.session_service import create_session, revoke_all

logger = logging.getLogger("agentcraft.account")

ROUTE_REQUEST = "/api/v2/account/deletion/request"
ROUTE_CANCEL = "/api/v2/account/deletion/cancel"

GRACE_DAYS = 14  # 注销宽限期（天）：deadline = now()+14d；cancel 令牌有效期同源

# 统一文案（契约钉死；模块级常量避免每次构造）
_PENDING_MESSAGE = "账户尚未完成邮箱验证"
_SUSPENDED_MESSAGE = "账户已被停用"  # T7 文案
_DELETING_409_MESSAGE = "注销处理中"  # request 端点 409 文案（brief 钉死；与 T7 403 文案区分）
_INVALID_CANCEL_MESSAGE = "撤销链接无效或已过期"  # cancel 统一防探测形态
_INVALID_CURRENT_MESSAGE = "当前密码不正确"  # 与改密端点统一（brief 钉死）

_CANCEL_TTL = timedelta(hours=VALID_HOURS["deletion_cancel"])  # 336h = 14 天

# request：active→deleting 跃迁（rowcount=1 复核；RETURNING 以 DB 时钟求剩余天数，
# 与 deadline 写入共用同一 now()——无应用/DB 时钟偏差面）
_DEACTIVATE_SQL = text(
    "UPDATE users SET status = 'deleting', "
    "deletion_deadline_at = now() + make_interval(days => :d) "
    "WHERE id = :u AND status = 'active' "
    "RETURNING ceil(extract(epoch from (deletion_deadline_at - now())) / 86400)::int "
    "AS days_remaining"
)

# cancel：deleting→active 恢复（deadline 置空；CHECK 约束同语句内自洽）
_REACTIVATE_SQL = text(
    "UPDATE users SET status = 'active', deletion_deadline_at = NULL "
    "WHERE id = :u AND status = 'deleting'"
)

# sweep：匿名化（凭据全灭 + email 不可回收；status/deleted_at/deadline 同语句收口）
_ANONYMIZE_SQL = text(
    "UPDATE users SET status = 'deleted', deleted_at = now(), deletion_deadline_at = NULL, "
    "password_hash = :h, mfa_secret_enc = NULL, "
    "email = 'deleted+' || id::text || '@users.invalid' "
    "WHERE id = :u AND status = 'deleting'"
)

# 单语句锁定读：行存活判定（expires_at > now()）在 DB 时钟内求值；user_id 谓词
# belt-and-braces（RLS 已按 GUC 限行）
_SELECT_TOKEN_FOR_UPDATE = text(
    "SELECT id, consumed_at, (expires_at > now()) AS live "
    "FROM account_action_tokens "
    "WHERE token_hash = :h AND purpose = 'deletion_cancel' AND user_id = :u FOR UPDATE"
)

_CONSUME_TOKEN_SQL = text(
    "UPDATE account_action_tokens SET consumed_at = now() WHERE id = :id AND consumed_at IS NULL"
)


async def _noop_terminate_tasks(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Phase 2 默认实现：no-op。任务终止/任务卷清理由任务域（Phase 6）注入替换。"""


TERMINATE_TASKS_HOOK: Callable[[AsyncSession, uuid.UUID], Awaitable[None]] = _noop_terminate_tasks
"""模块级任务终止钩子：request owner 事务内在 revoke_all 之后调用（任务域 Phase 6
替换为真实实现；Key 密文删除属 user_providers 域，Phase 3 已于 sweep 显式 DELETE，
不经此钩子）。"""


@dataclass(frozen=True)
class Replay:
    """幂等命中重放载荷：status_code + response_json 原样回放。"""

    status_code: int
    response_json: dict


@dataclass(frozen=True)
class CancelSuccess:
    """撤销成功产出：响应体 + 新会话明文（端点种双 cookie 用）。"""

    body: dict
    session_token: str


def _deleting_conflict() -> AgentCraftError:
    return AgentCraftError(ErrorCode.ACCOUNT_DELETING, _DELETING_409_MESSAGE, http_status=409)


def _invalid_cancel() -> AgentCraftError:
    """统一防探测错误：所有失效形态同码同文案同状态（响应体逐字节一致）。"""
    return AgentCraftError(ErrorCode.ACCOUNT_DELETING, _INVALID_CANCEL_MESSAGE, http_status=409)


def _invalid_current() -> AgentCraftError:
    return AgentCraftError(ErrorCode.INVALID_CREDENTIALS, _INVALID_CURRENT_MESSAGE, http_status=401)


def _gate_active(status: str) -> None:
    """request 状态门：仅 active 可发起注销；其余状态各自对应码（brief 钉死）。"""
    if status == "active":
        return
    if status == "pending":
        raise AgentCraftError(ErrorCode.ACCOUNT_PENDING, _PENDING_MESSAGE, http_status=409)
    if status == "suspended":
        raise AgentCraftError(ErrorCode.ACCOUNT_SUSPENDED, _SUSPENDED_MESSAGE, http_status=403)
    raise _deleting_conflict()  # deleting/deleted：统一 409 ACCOUNT_DELETING


def deletion_status_view(*, status: str, deadline_at: datetime | None) -> dict:
    """GET /account/deletion/status 载荷：deleting 且有 deadline 才计剩余天数
    （ceil((deadline-now)/86400)）；其余形态 days_remaining/deadline_at 均 null。"""
    if status == "deleting" and deadline_at is not None:
        remaining = math.ceil((deadline_at - datetime.now(timezone.utc)).total_seconds() / 86400)
        return {
            "status": status,
            "days_remaining": remaining,
            "deadline_at": deadline_at.isoformat(),
        }
    return {"status": status, "days_remaining": None, "deadline_at": None}


# ---------- request：再认证 → 宽限期跃迁 + 撤销令牌 + outbox 同事务 ----------


async def request_deletion(
    runtime: V2Runtime,
    *,
    user_id: str,
    status: str,
    password_hash: str,
    mfa_secret_enc: str | None,
    email: str,
    password: str,
    totp_code: str | None,
    idem_key: str,
    idem_hash: str,
) -> dict | Replay:
    """注销请求主流程（brief 步骤 1-5）；事务契约见模块 docstring。

    返回新成功响应体或 ``Replay``（幂等命中：原样重放）；状态门 409/403、再认证
    失败 401/400。凭据列（password_hash/mfa_secret_enc/email）取自认证上下文
    （get_v2_auth 同请求内 fresh 解析）。

    实际顺序为状态门 → TOTP → 验密 → begin（§7「重放优先于一切状态检查」在此
    刻意偏离）——偏离不可观测：成功请求 revoke_all 后全会话失效，携带同 key 的
    重放请求无法再通过认证达及本函数（get_v2_auth 先 401）；失败请求不写幂等
    记录，无可重放。
    """
    # 1. 状态门：仅 active 可发起注销
    _gate_active(status)

    # 2. TOTP 门（A15：先于 Argon2；失败计入 password_change_totp——同威胁同 scope）
    if mfa_secret_enc is not None:
        await _verify_change_totp(
            runtime, user_id=user_id, envelope=mfa_secret_enc, totp_code=totp_code
        )

    # 3. 当前密码验证（A10 再认证口径：密码必验）
    if not verify_password(password, password_hash):
        raise _invalid_current()

    # 4. 幂等 begin（app 会话，先于 owner 事务）：命中 → 原样重放（状态门/再认证
    #    已先行——偏离不可观测，见 docstring）
    subject = subject_user(user_id)
    async with runtime.app_factory() as db:
        replay = await begin(
            db, subject_hash=subject, route=ROUTE_REQUEST, key=idem_key, req_hash=idem_hash
        )
    if replay is not None:
        return Replay(status_code=replay["status_code"], response_json=replay["response_json"])

    # 5. owner 单事务：跃迁 → 全会话失效 → 任务钩子 → cancel 令牌 + outbox → 幂等 store
    owner_uuid = uuid.UUID(user_id)
    async with owner_session(runtime, user_id) as db:
        row = (await db.execute(_DEACTIVATE_SQL, {"u": user_id, "d": GRACE_DAYS})).first()
        if row is None:  # 并发已非 active → 统一 409（分文不写）
            raise _deleting_conflict()
        body = {"data": {"status": "deleting", "days_remaining": int(row.days_remaining)}}

        await revoke_all(db, owner_uuid)  # A10：含当前会话，全会话失效
        await TERMINATE_TASKS_HOOK(db, owner_uuid)  # 任务域 Phase 6 注入；现 no-op

        cancel_token = generate_token()
        db.add(
            AccountActionToken(
                user_id=owner_uuid,
                purpose="deletion_cancel",
                token_hash=hash_token(cancel_token),
                expires_at=datetime.now(timezone.utc) + _CANCEL_TTL,
            )
        )
        # 入队异常 → 整体回滚，绝不进入 deleting（transactional outbox 原子性）
        await enqueue(
            db,
            purpose="deletion_cancel",
            user_id=owner_uuid,
            recipient=email,
            action_token=cancel_token,
        )
        await store(
            db,
            subject_hash=subject,
            route=ROUTE_REQUEST,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=body,
        )
    return body


# ---------- cancel：分桶限流 → 恢复 active + 全新会话 ----------


async def cancel_deletion(
    runtime: V2Runtime,
    *,
    cancel_token: str,
    password: str,
    ip: str,
    device_label: str,
    idem_key: str,
    idem_hash: str,
) -> CancelSuccess | Replay:
    """注销撤销主流程（brief 步骤 1-4）；事务契约见模块 docstring。

    返回 ``CancelSuccess``（body + 新会话明文）或 ``Replay``（幂等命中：原样重放，
    不重复建会话）；全部失效形态统一抛 409（验密失败 401）。
    """
    # 1. 幂等 begin 最先（app 会话）：重放优先于一切状态检查（§7）
    subject = subject_token(cancel_token)
    async with runtime.app_factory() as db:
        replay = await begin(
            db, subject_hash=subject, route=ROUTE_CANCEL, key=idem_key, req_hash=idem_hash
        )
    if replay is not None:
        return Replay(status_code=replay["status_code"], response_json=replay["response_json"])

    # 2. admin 预检（鸡蛋问题：user_id 取自令牌行）；purpose 过滤钉死——跨 purpose
    #    令牌（email_verify/password_reset）按未命中处理（探测桶计数）
    token_hash = hash_token(cancel_token)
    async with runtime.admin_factory() as db:
        token_row = (
            await db.execute(
                text(
                    "SELECT user_id FROM account_action_tokens "
                    "WHERE token_hash = :h AND purpose = 'deletion_cancel'"
                ),
                {"h": token_hash},
            )
        ).first()

    if token_row is None:
        # 未命中：探测桶限流（自管事务独立提交）后统一 409——计数不因拒绝路径丢失
        async with runtime.app_factory() as db:
            await enforce(db, scope="deletion_cancel_invalid", subjects=[hmac_subject("ip", ip)])
        raise _invalid_cancel()

    # 3. 命中：合法路径桶 [HMAC(user_id), HMAC(ip)]（与探测桶分账，T11 per-subject 语义）
    user_id = str(token_row.user_id)
    async with runtime.app_factory() as db:
        await enforce(
            db,
            scope="deletion_cancel_attempt",
            subjects=[hmac_subject("user", user_id), hmac_subject("ip", ip)],
        )

    # 4. owner 事务（GUC=令牌行 user_id）：锁定重校验 → 状态门 → 验密 → 跃迁 →
    #    消费 → 新会话 → 幂等 store（校验失败即回滚，分文不消费）
    async with owner_session(runtime, user_id) as db:
        row = (await db.execute(_SELECT_TOKEN_FOR_UPDATE, {"h": token_hash, "u": user_id})).first()
        if row is None or row.consumed_at is not None or not row.live:
            raise _invalid_cancel()  # 过期/已消费/不可见 → 统一 409

        user = (
            await db.execute(
                text("SELECT status, password_hash, email, role FROM users WHERE id = :u"),
                {"u": user_id},
            )
        ).first()
        if user is None or user.status != "deleting":
            raise _invalid_cancel()  # 非 deleting（并发变动）→ 统一 409，令牌不消费
        if not verify_password(password, user.password_hash):
            raise _invalid_current()  # 与改密端点统一 401 文案

        restored = await db.execute(_REACTIVATE_SQL, {"u": user_id})
        if restored.rowcount != 1:  # belt-and-braces（上句状态门已过）
            raise _invalid_cancel()

        consumed = await db.execute(_CONSUME_TOKEN_SQL, {"id": row.id})
        if consumed.rowcount != 1:  # belt-and-braces（行已 FOR UPDATE 锁定）
            raise _invalid_cancel()

        session_token, csrf_token = await create_session(
            db, user_id=uuid.UUID(user_id), device_label=device_label
        )
        body = {
            "data": {
                "user": {
                    "id": user_id,
                    "email": user.email,
                    "role": user.role,
                    "status": "active",
                },
                "csrf_token": csrf_token,
            }
        }
        await store(
            db,
            subject_hash=subject,
            route=ROUTE_CANCEL,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=body,
        )
    return CancelSuccess(body=body, session_token=session_token)


# ---------- sweep：到期匿名化清理（lifespan 后台作业）----------


async def sweep_expired(runtime: V2Runtime) -> int:
    """到期清理：deleting 且 deadline <= now() → deleted + 匿名化 + user_providers
    密文行显式删除（D15）+ 全会话失效。

    admin 会话圈定候选（users_admin_read 全量只读），逐用户 owner 事务匿名化
    （GUC=user）后显式删 user_providers 行；UPDATE rowcount=0（并发 cancel 恢复）
    → 跳过且不撤销其会话。返回处理数（rowcount=1 的用户数）。
    """
    async with runtime.admin_factory() as db:
        expired = (
            (
                await db.execute(
                    text(
                        "SELECT id FROM users "
                        "WHERE status = 'deleting' AND deletion_deadline_at <= now()"
                    )
                )
            )
            .scalars()
            .all()
        )

    processed = 0
    for user_id in expired:
        # 匿名哈希在事务外预算（昂贵 CPU 不持行锁，T12 同纪律）
        random_hash = hash_password(generate_token())
        async with owner_session(runtime, str(user_id)) as db:
            anonymized = await db.execute(_ANONYMIZE_SQL, {"h": random_hash, "u": str(user_id)})
            if anonymized.rowcount != 1:
                continue  # 并发 cancel 恢复 → 分文不写，且不得误杀其新会话
            # Key 密文删除属 user_providers 域（DB Design §157；裁决 D15）。users 行
            # 不删除 → user_id CASCADE 永不触发，必须显式删。Phase 6 接线
            # TERMINATE_TASKS_HOOK 时任务清理必须先于此 DELETE（tasks.provider_id
            # FK RESTRICT）。
            await db.execute(
                text("DELETE FROM user_providers WHERE user_id = :u"), {"u": str(user_id)}
            )
            await revoke_all(db, user_id)
            processed += 1
    return processed


async def deletion_sweep_loop(runtime: V2Runtime, poll_seconds: float = 60) -> None:
    """常驻到期清理循环（lifespan 后台协程，仅 V2 模式）：每轮 sweep_expired 后睡
    poll_seconds。单轮异常只记录不外抛（DB 抖动不得杀死进程）；仅 CancelledError
    穿透供 lifespan 关停取消（outbox_loop 同款形态）。"""
    while True:
        try:
            swept = await sweep_expired(runtime)
            if swept:
                logger.info("deletion sweep cycle processed=%d", swept)
        except Exception:
            logger.exception("deletion sweep cycle failed; will retry")
        await asyncio.sleep(poll_seconds)
