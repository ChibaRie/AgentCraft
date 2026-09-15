"""admin 邀请管理服务（Phase 7 T2；Sup §6:132-134）：创建/列表/撤销。

调用契约（调用方必读）：

- ``admin_db`` 为调用方已 begin 的 admin 会话（端点壳 ``admin_factory().begin()``
  建，同 review_service 形态）；邀请行 + 邀请邮件 outbox 行 + 审计行**同事务**收口
  （Sup §6:126 审计失败即回滚），任一失败整体回滚零残留；
- token 明文（token_urlsafe(32)）只存 SHA-256 哈希（invitations.token_hash）并只进
  outbox payload（信封加密）——返回值/审计**零 token**（Sup:132 红线）；
- 创建冲突：invitations_one_open_email（consumed_at/revoked_at 双 NULL 的部分唯一
  索引）冲突 → 409 INVITATION_INVALID（先查后插 + flush IntegrityError 兜底）；
- 撤销状态语义（T2 申报）：仅未消费可撤——consumed → 409 INVITATION_CONSUMED、
  已撤销 → 409 INVITATION_INVALID；**过期未消费未撤销可撤销**（置 revoked）——
  部分唯一索引谓词不含 expires_at，过期行仍占用邮箱槽，revoke 是管理员释放该槽
  的唯一杠杆，且 Sup:134 仅以「未消费」为界；
- 列表状态判定（SQL 单时钟 now() 求值，过滤与行值共用同一 case 表达式）：
  consumed（consumed_at 非空）> revoked（revoked_at 非空）> open（未过期）>
  expired；open = 未消费未撤销未过期，终态优先于 expired。

outbox 落行不消费 ``outbox.enqueue``：其 user_id 形参强制 owner id，而邀请场景
无 user 行、行值须为 NULL（D4③ email_outbox_admin_insert 承载形态）——故复用
enqueue 的 payload 白名单构造与信封加密原语（_build_payload/_outbox_aad/
_outbox_keyring 单一事实源）就地落行。
"""

import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.utils.crypto import encrypt_text
from backend.v2.author_service import _reject_invalid_uuid as _parse_uuid
from backend.v2.ids import uuid7
from backend.v2.invitation_service import normalize_email
from backend.v2.models import AuditLog, EmailOutbox, Invitation
from backend.v2.outbox import _build_payload, _outbox_aad, _outbox_keyring
from backend.v2.security import generate_token, hash_token

_MAX_EXPIRES_IN_DAYS = 7
_LIST_PAGE_MAX = 100
_STATUS_FILTERS = frozenset({"open", "consumed", "expired", "revoked"})


def _validation(message: str) -> HTTPException:
    # VALIDATION_ERROR 为 V1 约定形状字面（不经 ErrorCode 注册表，同 author_service）
    return HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": message})


def _unified_404() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "邀请不存在"})


def _duplicate_open() -> AgentCraftError:
    return AgentCraftError(
        ErrorCode.INVITATION_INVALID, "该邮箱已有未消费且未撤销的邀请", http_status=409
    )


def _enqueue_invitation_mail(
    admin_db: AsyncSession, invitation_id: _uuid.UUID, recipient: str, token: str
) -> None:
    """邀请邮件 outbox 行（user_id=NULL；0009 email_outbox_admin_insert 承载）。

    payload 走 outbox 白名单构造（template_id/recipient/vars，token 明文仅在
    vars.action_token），信封加密 AAD 绑定本行 id；不 flush——统一由调用方单次
    flush 收口（唯一冲突一并转 409）。
    """
    oid = uuid7()
    payload = _build_payload("invitation", recipient, token)
    active_kid, keyring = _outbox_keyring()
    envelope = encrypt_text(
        json.dumps(payload, ensure_ascii=False),
        aad=_outbox_aad(oid),
        keyring=keyring,
        active_kid=active_kid,
    )
    admin_db.add(
        EmailOutbox(
            id=oid,
            user_id=None,
            purpose="invitation",
            payload_ciphertext=json.dumps(envelope),
            state="pending",
        )
    )


async def create_invitation(
    admin_db: AsyncSession,
    *,
    admin_id: str,
    email: str,
    expires_in_days: int,
    reason: str,
    request_id: str | None,
) -> dict:
    """创建邀请（201 语义）：邀请行 + outbox 行 + 审计行同事务；返回邀请元数据。

    expires_in_days 须为 1..7（>7 → 400 VALIDATION_ERROR，brief 钉死；<1 同罚——
    非正天数即过期语义，无业务意义）；expires_at = now + expires_in_days。
    审计 invitation.create detail 仅 invitation_id/email/expires_in_days——无 token。
    """
    if (
        isinstance(expires_in_days, bool)
        or not isinstance(expires_in_days, int)
        or not 1 <= expires_in_days <= _MAX_EXPIRES_IN_DAYS
    ):
        raise _validation(f"expires_in_days 须为 1..{_MAX_EXPIRES_IN_DAYS} 的整数（>7 → 400）")
    try:
        normalized = normalize_email(email)
    except ValueError as exc:  # EmailNotValidError（email_validator）
        raise _validation("email 格式非法") from exc

    existing = (
        await admin_db.execute(
            select(Invitation.id).where(
                Invitation.email == normalized,
                Invitation.consumed_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:  # invitations_one_open_email 谓词同款预检
        raise _duplicate_open()

    token = generate_token()  # token_urlsafe(32)，明文不返回不审计
    invitation_id = uuid7()
    expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
    admin_db.add(
        Invitation(
            id=invitation_id,
            token_hash=hash_token(token),
            email=normalized,
            expires_at=expires_at,
            created_by=_uuid.UUID(admin_id),
        )
    )
    _enqueue_invitation_mail(admin_db, invitation_id, normalized, token)
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action="invitation.create",
            target_type="invitation",
            target_id=invitation_id,
            reason=reason,
            request_id=request_id,
            detail={
                "invitation_id": str(invitation_id),
                "email": normalized,
                "expires_in_days": expires_in_days,
            },
        )
    )
    try:
        await admin_db.flush()
    except IntegrityError as exc:
        # 并发同邮箱撞部分唯一索引（预检后的竞态窗口）——整体回滚转 409
        raise _duplicate_open() from exc
    return {
        "id": str(invitation_id),
        "email": normalized,
        "expires_at": expires_at.isoformat(),
    }


async def list_invitations(
    admin_db: AsyncSession,
    *,
    status: str | None,
    page: int,
    size: int,
) -> dict:
    """邀请列表（状态过滤+分页；created_at DESC + id DESC 稳定序，Sup §7 信封）。

    过滤与行状态共用同一 SQL case 表达式（DB now() 单时钟求值）；status 词表外 /
    分页非法 → 400 VALIDATION_ERROR。返回 ``{"items", "total", "page", "size"}``。
    """
    if page < 1 or size < 1 or size > _LIST_PAGE_MAX:
        raise _validation("分页参数非法（page≥1，1≤size≤100）")
    if status is not None and status not in _STATUS_FILTERS:
        raise _validation("status 仅支持 open/consumed/expired/revoked")
    status_expr = case(
        (Invitation.consumed_at.is_not(None), "consumed"),
        (Invitation.revoked_at.is_not(None), "revoked"),
        (Invitation.expires_at > func.now(), "open"),
        else_="expired",
    )
    conds = [status_expr == status] if status is not None else []
    total = int(
        (
            await admin_db.execute(select(func.count()).select_from(Invitation).where(*conds))
        ).scalar_one()
    )
    rows = (
        await admin_db.execute(
            select(Invitation, status_expr.label("status"))
            .where(*conds)
            .order_by(Invitation.created_at.desc(), Invitation.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    items = [_invitation_brief(inv, row_status) for inv, row_status in rows]
    return {"items": items, "total": total, "page": page, "size": size}


def _invitation_brief(inv: Invitation, status: str) -> dict:
    """邀请行 → 列表项（时间戳 isoformat；零 token/token_hash 面）。"""
    return {
        "id": str(inv.id),
        "email": inv.email,
        "status": status,
        "created_at": inv.created_at.isoformat(),
        "expires_at": inv.expires_at.isoformat(),
        "consumed_at": inv.consumed_at.isoformat() if inv.consumed_at else None,
        "revoked_at": inv.revoked_at.isoformat() if inv.revoked_at else None,
    }


async def revoke_invitation(
    admin_db: AsyncSession,
    *,
    admin_id: str,
    invitation_id: str,
    reason: str,
    request_id: str | None,
) -> dict:
    """撤销未消费邀请（Sup:134）：FOR UPDATE 锁行 → 状态门 → 置 revoked_at + 审计。

    consumed → 409 INVITATION_CONSUMED；已撤销 → 409 INVITATION_INVALID；不存在
    → 统一 404；过期未消费未撤销**可撤销**（模块 docstring 申报：释放邮箱槽的唯一
    杠杆）。行值真实变化由调用方事务提交落地。
    """
    iid = _parse_uuid(invitation_id, "invitation_id")
    inv = (
        await admin_db.execute(select(Invitation).where(Invitation.id == iid).with_for_update())
    ).scalar_one_or_none()
    if inv is None:
        raise _unified_404()
    if inv.consumed_at is not None:
        raise AgentCraftError(
            ErrorCode.INVITATION_CONSUMED, "邀请已被消费，无法撤销", http_status=409
        )
    if inv.revoked_at is not None:
        raise AgentCraftError(ErrorCode.INVITATION_INVALID, "邀请已撤销", http_status=409)
    was_expired = inv.expires_at <= datetime.now(timezone.utc)
    inv.revoked_at = datetime.now(timezone.utc)
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action="invitation.revoke",
            target_type="invitation",
            target_id=inv.id,
            reason=reason,
            request_id=request_id,
            detail={
                "invitation_id": str(inv.id),
                "email": inv.email,
                "was_expired": was_expired,
            },
        )
    )
    await admin_db.flush()
    return {
        "id": str(inv.id),
        "email": inv.email,
        "status": "revoked",
        "revoked_at": inv.revoked_at.isoformat(),
    }
