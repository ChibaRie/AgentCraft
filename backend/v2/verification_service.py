"""邮箱验证 confirm/resend 服务（Task 10）：令牌单次消费与验证邮件重发。

契约出处：task-10-brief + Supplement §2/§7。事务契约（调用方必读）：

- confirm（公开端点，无会话无 CSRF）：**无限流**——契约未登记阈值（A5 未列入、
  无文档化限额），滥用面由令牌属性约束（token_urlsafe(32) 熵 + 72h 有效期 +
  单次消费 + token_hash 唯一索引，枚举不可行）；幂等 ``begin`` 最先（重放优先于
  一切状态检查——同 key 命中原样重放原 200，无论令牌当前已消费与否，§7）；
- 预检走 admin 会话（只读）：按 token_hash + purpose='email_verify' 定位令牌行取
  user_id（owner GUC 的鸡蛋问题同 resolve_session）；未命中 → 统一 400；
- 业务写入在 ``owner_session(runtime, user_id)`` **单事务**内收口（GUC=令牌行
  user_id）：令牌 FOR UPDATE 重校验（未消费未过期，DB 时钟求值）→ users
  pending→active 跃迁（rowcount=0 时复查状态：已 active → 幂等成功、令牌仍消费；
  suspended/deleting/deleted → 统一 400，事务回滚分文不消费）→ 消费令牌 → 幂等
  ``store``，任一失败整体回滚；
- resend（认证端点，get_v2_auth 已过会话/CSRF/状态门）：限流 ``enforce``（app
  会话自管事务独立提交，先于状态门与业务，被拒尝试同样计入窗口）→ 状态门（仅
  pending，否则 409 VALIDATION_ERROR——V1 约定形状，不经 ErrorCode 注册表）→
  owner 事务：旧未消费 email_verify 令牌全部作废（防堆积）→ 新令牌（72h）+
  outbox 同事务原子落库（transactional outbox）。非幂等键控端点（A5 未列入），
  以限流约束重试频率。

防枚举：confirm 全部失效形态（不存在/错 purpose/过期/已消费/非 pending 状态）统一
400 ``EMAIL_NOT_VERIFIED``「验证链接无效或已过期」——同码同文案同状态，响应体逐
字节一致。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.idempotency import begin, store, subject_token
from backend.v2.models import AccountActionToken
from backend.v2.outbox import VALID_HOURS, enqueue
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, owner_session
from backend.v2.security import generate_token, hash_token

ROUTE_CONFIRM = "/api/v2/auth/email-verification/confirm"
ROUTE_RESEND = "/api/v2/auth/email-verification/resend"
_INVALID_MESSAGE = "验证链接无效或已过期"
# V1 约定形状（VALIDATION_ERROR 不在 ErrorCode 注册表；契约钉死）
_RESEND_WRONG_STATUS_DETAIL = {
    "code": "VALIDATION_ERROR",
    "message": "当前账户状态无法重发验证邮件",
}

# 单语句锁定读：行存活判定（expires_at > now()）在 DB 时钟内求值，与消费 UPDATE 的
# now() 谓词共用同一时钟基准；user_id 谓词 belt-and-braces（RLS 已按 GUC 限行）
_SELECT_TOKEN_FOR_UPDATE = text(
    "SELECT id, consumed_at, (expires_at > now()) AS live "
    "FROM account_action_tokens "
    "WHERE token_hash = :h AND purpose = 'email_verify' AND user_id = :u FOR UPDATE"
)

_CONSUME_TOKEN_SQL = text(
    "UPDATE account_action_tokens SET consumed_at = now() WHERE id = :id AND consumed_at IS NULL"
)

_ACTIVATE_SQL = text("UPDATE users SET status = 'active' WHERE id = :u AND status = 'pending'")

# resend：旧未消费 email_verify 令牌整批作废（防堆积；新令牌另行 INSERT）
_INVALIDATE_PRIOR_SQL = text(
    "UPDATE account_action_tokens SET consumed_at = now() "
    "WHERE user_id = :u AND purpose = 'email_verify' AND consumed_at IS NULL"
)


def _invalid() -> AgentCraftError:
    """统一防枚举错误：所有失效形态同码同文案同状态（响应体逐字节一致）。"""
    return AgentCraftError(ErrorCode.EMAIL_NOT_VERIFIED, _INVALID_MESSAGE, http_status=400)


@dataclass(frozen=True)
class Replay:
    """幂等命中重放载荷：status_code + response_json 原样回放。"""

    status_code: int
    response_json: dict


async def _issue_email_verify(db: AsyncSession, user_id, recipient: str) -> None:
    """email_verify 令牌（72h，与 outbox 模板有效期同源）+ 事务性发件箱入队。"""
    verify_token = generate_token()
    db.add(
        AccountActionToken(
            user_id=user_id,
            purpose="email_verify",
            token_hash=hash_token(verify_token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=VALID_HOURS["email_verify"]),
        )
    )
    await enqueue(
        db, purpose="email_verify", user_id=user_id, recipient=recipient, action_token=verify_token
    )


async def confirm_email_verification(
    runtime: V2Runtime, *, verify_token: str, idem_key: str, idem_hash: str
) -> dict | Replay:
    """邮箱验证确认主流程（brief 步骤 1-3）；事务契约见模块 docstring。

    返回新成功响应体（pending→active 与已 active 两成功路径共用同一载荷）或
    ``Replay``（幂等命中：原样重放）；全部失效形态统一抛 400 ``AgentCraftError``。
    """
    # 1. 幂等 begin 最先（app 会话）：命中且 request_hash 一致 → 原样重放，业务零
    #    副作用——重放优先于一切状态检查（同 key 即令牌已消费也回放原 200）
    subject = subject_token(verify_token)
    async with runtime.app_factory() as db:
        replay = await begin(
            db, subject_hash=subject, route=ROUTE_CONFIRM, key=idem_key, req_hash=idem_hash
        )
    if replay is not None:
        return Replay(status_code=replay["status_code"], response_json=replay["response_json"])

    # 2. admin 预检（鸡蛋问题：user_id 取自令牌行，owner GUC 依赖它）；admin role 对
    #    account_action_tokens 仅 *_admin_read SELECT——只读定位，校验留给 owner 事务
    async with runtime.admin_factory() as db:
        token_row = (
            await db.execute(
                text(
                    "SELECT user_id FROM account_action_tokens "
                    "WHERE token_hash = :h AND purpose = 'email_verify'"
                ),
                {"h": hash_token(verify_token)},
            )
        ).first()
    if token_row is None:
        raise _invalid()

    # 3. owner 事务（GUC=令牌行 user_id）：锁定重校验 → 状态跃迁 → 消费 → 幂等 store
    user_id = str(token_row.user_id)
    body = {"data": {"status": "active"}}
    async with owner_session(runtime, user_id) as db:
        row = (
            await db.execute(
                _SELECT_TOKEN_FOR_UPDATE, {"h": hash_token(verify_token), "u": user_id}
            )
        ).first()
        if row is None or row.consumed_at is not None or not row.live:
            raise _invalid()  # 过期/已消费/不可见 → 统一 400；事务回滚分文不消费

        transitioned = await db.execute(_ACTIVATE_SQL, {"u": user_id})
        if transitioned.rowcount == 0:
            status = (
                await db.execute(text("SELECT status FROM users WHERE id = :u"), {"u": user_id})
            ).scalar_one()
            if status != "active":
                raise _invalid()  # suspended/deleting/deleted → 回滚，令牌不消费
            # 已是 active（重复点击）→ 幂等成功：令牌仍消费，返回同一 200

        consumed = await db.execute(_CONSUME_TOKEN_SQL, {"id": row.id})
        if consumed.rowcount != 1:  # belt-and-braces（行已 FOR UPDATE 锁定）
            raise _invalid()

        # 幂等 store 同事务原子落库（status_code=200；两成功路径共用同一载荷）
        await store(
            db,
            subject_hash=subject,
            route=ROUTE_CONFIRM,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=body,
        )
    return body


async def resend_email_verification(
    runtime: V2Runtime, *, user_id: str, recipient: str, status: str
) -> None:
    """验证邮件重发主流程；事务契约见模块 docstring。

    状态门以认证上下文（get_v2_auth 同请求内 fresh 解析）的用户状态裁决；限流先于
    状态门——被状态门拒绝的尝试同样计入窗口（T9 同款纪律）。
    """
    # 1. 限流（app 会话独立提交；主体 [HMAC(user_id)]，3/h）
    async with runtime.app_factory() as db:
        await enforce(
            db, scope="email_verify_resend", subjects=[hmac_subject("user", str(user_id))]
        )

    # 2. 状态门：仅 pending 用户可重发（409 VALIDATION_ERROR，V1 约定形状）
    if status != "pending":
        raise HTTPException(status_code=409, detail=dict(_RESEND_WRONG_STATUS_DETAIL))

    # 3. owner 事务：旧未消费令牌整批作废 → 新令牌 + outbox 同事务原子落库
    async with owner_session(runtime, str(user_id)) as db:
        await db.execute(_INVALIDATE_PRIOR_SQL, {"u": str(user_id)})
        await _issue_email_verify(db, user_id, recipient)
