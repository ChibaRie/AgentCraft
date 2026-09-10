"""邀请接受服务（Task 9）：并发安全消费邀请并事务性落建账户。

契约出处：task-9-brief + Supplement §2/§7。事务契约（调用方必读）：

- 限流 ``enforce``（app 会话，自管事务独立提交）与幂等 ``begin``（app 会话）先于
  业务：429 即短路且业务零副作用；begin 命中一致 → 返回 ``Replay`` 原样重放
  （重放不携带 Set-Cookie——契约钉死）；
- 业务写入全部在 ``owner_session(runtime, new_id)`` **单事务**内收口（GUC = 预生成
  uuid7）：邀请行 FOR UPDATE 消费 → users INSERT → create_session → email_verify
  令牌 + outbox 入队 → 幂等 ``store``，任一失败整体回滚；
- 并发语义：两请求同 token 并发——后者在 FOR UPDATE 上等待，前者提交后（READ
  COMMITTED 重读）必见 consumed_at IS NOT NULL → 统一 409（PRD §6：恰一 user 行）；
- 防枚举：不存在/过期/已消费/已撤销/邮箱不符/邮箱重复一律统一 409
  INVITATION_INVALID「邀请无效或已过期」；仅邮箱不符（邀请其余有效）先经 admin
  会话独立事务写 audit_logs('invitation.email_mismatch')——审计独立于业务回滚存续。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from email_validator import validate_email
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.idempotency import begin, store, subject_token
from backend.v2.ids import uuid7
from backend.v2.models import AccountActionToken, AuditLog, User
from backend.v2.outbox import VALID_HOURS, enqueue
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, owner_session
from backend.v2.security import generate_token, hash_password, hash_token
from backend.v2.session_service import create_session

ROUTE = "/api/v2/auth/invitations/accept"
_INVALID_MESSAGE = "邀请无效或已过期"

# 单语句锁定读：行存活判定（expires_at > now()）在 DB 时钟内求值，与消费 UPDATE 的
# now() 谓词共用同一时钟基准
_SELECT_FOR_UPDATE = text(
    "SELECT id, email, consumed_at, revoked_at, (expires_at > now()) AS live "
    "FROM invitations WHERE token_hash = :h FOR UPDATE"
)


def _invalid() -> AgentCraftError:
    """统一防枚举错误：所有失效形态同码同文案同状态（响应体逐字节一致）。"""
    return AgentCraftError(ErrorCode.INVITATION_INVALID, _INVALID_MESSAGE, http_status=409)


@dataclass(frozen=True)
class Replay:
    """幂等命中重放载荷：status_code + response_json 原样回放（无 Set-Cookie）。"""

    status_code: int
    response_json: dict


@dataclass(frozen=True)
class AcceptResult:
    """新成功路径产出：响应体 + 会话明文（端点种 cookie 用；幂等载荷刻意不含 cookie）。"""

    body: dict
    session_token: str


def normalize_email(email: str) -> str:
    """email-validator 语法门 + 小写规范化。

    brief 钉死：输入先规范化为小写再与 invitations.email 精确匹配；库内 normalized
    已将域名小写（IDNA），本地部统一小写收口——users.email 唯一约束大小写敏感，
    全小写入库消除同址异形双账号面。非法地址抛 EmailNotValidError（ValueError）。
    """
    return validate_email(email.strip(), check_deliverability=False).normalized.lower()


async def _audit_email_mismatch(admin_factory: async_sessionmaker, invitation_id: object) -> None:
    """邮箱不符留痕：admin 会话独立事务（audit_logs 无 RLS 且 app role 零授权）。

    独立提交使审计行在随后业务事务回滚后依然存续（失败取证语义）。
    """
    async with admin_factory() as admin_db:
        async with admin_db.begin():
            admin_db.add(
                AuditLog(
                    action="invitation.email_mismatch",
                    target_type="invitation",
                    target_id=invitation_id,
                    reason="accept email mismatch",
                    actor_id=None,
                )
            )


async def _lock_and_validate(
    runtime: V2Runtime, db: AsyncSession, invitation_token: str, normalized: str
):
    """FOR UPDATE 锁定邀请行并校验；无效（含邮箱不符）→ 统一 409。

    返回邀请行（仅当存在且未消费、未撤销、未过期且邮箱精确匹配）；邮箱不符且邀请
    其余有效时先经 admin 会话写审计再抛（brief 步骤 4）。
    """
    inv = (await db.execute(_SELECT_FOR_UPDATE, {"h": hash_token(invitation_token)})).first()
    if inv is not None and inv.consumed_at is None and inv.revoked_at is None and inv.live:
        if inv.email != normalized:
            await _audit_email_mismatch(runtime.admin_factory, inv.id)
            raise _invalid()
        return inv
    raise _invalid()


async def _persist_account(
    db: AsyncSession, new_id, normalized: str, password: str, device_label: str
) -> tuple[str, str]:
    """owner 事务内落建 pending 用户并同事务建会话；返回 (会话明文, csrf 明文)。

    两者明文均仅此一次（会话明文供端点种 cookie，csrf 明文入响应体）。
    users INSERT 命中 owner-RLS WITH CHECK（id = GUC）；email 唯一冲突（另一有效
    邀请先到）→ 统一 409，本事务随异常回滚、邀请保持未消费。
    """
    db.add(
        User(
            id=new_id,
            email=normalized,
            password_hash=hash_password(password),
            role="user",
            status="pending",
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise _invalid() from exc
    return await create_session(db, user_id=new_id, device_label=device_label)


async def _issue_email_verify(db: AsyncSession, new_id, recipient: str) -> None:
    """email_verify 令牌（72h，与 outbox 模板有效期同源）+ 事务性发件箱入队。"""
    verify_token = generate_token()
    db.add(
        AccountActionToken(
            user_id=new_id,
            purpose="email_verify",
            token_hash=hash_token(verify_token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=VALID_HOURS["email_verify"]),
        )
    )
    await enqueue(
        db, purpose="email_verify", user_id=new_id, recipient=recipient, action_token=verify_token
    )


async def accept_invitation(
    runtime: V2Runtime,
    *,
    invitation_token: str,
    email: str,
    password: str,
    idem_key: str,
    idem_hash: str,
    ip: str,
    device_label: str,
) -> AcceptResult | Replay:
    """邀请接受主流程（brief 步骤 1-9）；事务契约见模块 docstring。

    返回 ``AcceptResult``（新成功：响应体 + 会话明文供端点种双 cookie）或
    ``Replay``（幂等命中：原样重放）；业务失败统一抛 409 ``AgentCraftError``。
    """
    # 1. 限流（app 会话独立提交；任何形态的失败尝试均计入窗口）
    async with runtime.app_factory() as db:
        await enforce(db, scope="invitation_accept", subjects=[hmac_subject("ip", ip)])

    # 2. 幂等 begin（app 会话）：命中且 request_hash 一致 → 原样重放，业务零副作用
    subject = subject_token(invitation_token)
    async with runtime.app_factory() as db:
        replay = await begin(
            db, subject_hash=subject, route=ROUTE, key=idem_key, req_hash=idem_hash
        )
    if replay is not None:
        return Replay(status_code=replay["status_code"], response_json=replay["response_json"])

    normalized = normalize_email(email)
    new_id = uuid7()  # GUC 预生成：users INSERT WITH CHECK（id = owner）由它满足
    async with owner_session(runtime, str(new_id)) as db:
        inv = await _lock_and_validate(runtime, db, invitation_token, normalized)
        session_token, csrf_token = await _persist_account(
            db, new_id, normalized, password, device_label
        )

        # 消费（行已 FOR UPDATE 锁定；rowcount 复核为 belt-and-braces）
        consumed = await db.execute(
            text(
                "UPDATE invitations SET consumed_at = now() "
                "WHERE id = :id AND consumed_at IS NULL AND revoked_at IS NULL"
            ),
            {"id": inv.id},
        )
        if consumed.rowcount != 1:
            raise _invalid()

        await _issue_email_verify(db, new_id, normalized)

        # 幂等 store 同事务原子落库（Set-Cookie 刻意不入重放载荷——契约）
        body = {
            "data": {
                "user_id": str(new_id),
                "email": normalized,
                "status": "pending",
                "csrf_token": csrf_token,
            }
        }
        await store(
            db,
            subject_hash=subject,
            route=ROUTE,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=body,
        )
    return AcceptResult(body=body, session_token=session_token)
