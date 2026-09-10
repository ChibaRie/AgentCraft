"""邮件 outbox（事务性发件箱）+ 租约派发器（task-8-brief；Supplement §9.3）。

事务契约：``enqueue`` **不提交**——必须在调用方 owner 事务内调用（owner GUC 已设），
outbox 行与业务写入同事务原子落库/回滚（transactional outbox）；派发器
``dispatch_once`` 以 admin role 独立认领（0003 的 email_outbox_admin_update 策略），
认领与终态分属两个短事务——transport.send 恒在事务外执行，慢传输不拖长锁持有。

并发与崩溃恢复：认领用 FOR UPDATE SKIP LOCKED + 5 分钟租约（lease_owner/lease_expires_at），
多实例并发互不双认领；worker 崩溃后租约过期行可被重新认领。投递语义为**至少一次**
（at-least-once）：崩溃重放可能重复发送，接收方需容忍。

密钥：payload 信封加密每次调用现读 ``Settings.EMAIL_OUTBOX_ENCRYPTION_KEY``（不缓存，
轮换即时生效）；AAD 绑定行 id（``agentcraft:email_outbox:{outbox_id}:v1``），解密侧
必须以行 id 重建 AAD，跨行搬用密文必然解密失败。

日志红线：dispatcher 日志只含 outbox id/purpose/attempts 等元数据，不落 payload 内容
（收件人/令牌）；attempts 达上限记 ERROR（管理员告警语义），重试失败记 WARNING。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

from email_validator import validate_email
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.utils.crypto import decrypt_text, encrypt_text, make_keyring
from backend.v2.ids import uuid7
from backend.v2.models import EmailOutbox
from backend.v2.runtime import V2Runtime

logger = logging.getLogger("agentcraft.mail")

# purpose → 链接有效期（小时）。钉死契约值；_build_payload 以此校验 purpose 合法性
VALID_HOURS: dict[str, float] = {
    "invitation": 168,
    "email_verify": 72,
    "password_reset": 0.5,
    "deletion_cancel": 336,
}
MAX_ATTEMPTS = 5
LEASE_MINUTES = 5
# 第 attempts 次失败后的重试延迟秒数（attempts ∈ 1..MAX_ATTEMPTS-1）；达 MAX_ATTEMPTS 终态
BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 900, 1800)
assert len(BACKOFF_SECONDS) == MAX_ATTEMPTS - 1  # 退避表必须覆盖全部重试档位

# payload 白名单（Eng §2.2 红线：无 URL/header/正文/附件）——多余字段在构造处拒绝
_PAYLOAD_TOP_KEYS = frozenset({"template_id", "recipient", "vars"})
_PAYLOAD_VARS_KEYS = frozenset({"action_token", "valid_hours"})
_AAD_PREFIX = "agentcraft:email_outbox:"


def _outbox_aad(outbox_id: uuid.UUID) -> str:
    """AAD 绑定 outbox 行 id：解密必须以同一行 id 重建（防跨行搬用密文）。"""
    return f"{_AAD_PREFIX}{outbox_id}:v1"


def _normalize_email(recipient: str) -> str:
    """收件人规范化：去首尾空白 + 域名小写（email-validator 语法校验，非法即拒）。

    email_validator 的 EmailNotValidError 继承 ValueError——构造处 fail fast。
    """
    return validate_email(recipient.strip(), check_deliverability=False).normalized


def _build_payload(
    purpose: str,
    recipient: str,
    action_token: str,
    *,
    extra_vars: dict[str, object] | None = None,
) -> dict:
    """构造严格白名单 payload；未知 purpose / 白名单外 vars 一律 ValueError。

    未来调用方若想携带额外模板变量，必须先在 _PAYLOAD_VARS_KEYS 登记白名单键
    （构造处即拒绝未知键——Eng §2.2：邮件只允许 template_id + 收件人 + 受控变量）。
    """
    if purpose not in VALID_HOURS:
        raise ValueError(f"未知 outbox purpose: {purpose}")
    if extra_vars:
        unknown = sorted(set(extra_vars) - _PAYLOAD_VARS_KEYS)
        if unknown:
            raise ValueError(f"payload vars 白名单外字段: {unknown}")
    payload = {
        "template_id": purpose,
        "recipient": _normalize_email(recipient),
        "vars": {"action_token": action_token, "valid_hours": VALID_HOURS[purpose]},
    }
    # 结构性护栏：未来编辑使字段越界时在此失败，而非静默外泄
    if set(payload) != _PAYLOAD_TOP_KEYS or set(payload["vars"]) != _PAYLOAD_VARS_KEYS:
        raise ValueError("payload 字段超出白名单")
    return payload


def _outbox_keyring() -> tuple[str, dict[str, bytes]]:
    """现读 EMAIL_OUTBOX_ENCRYPTION_KEY（b64url 32B）→ (active_kid, keyring)。"""
    raw = get_settings().EMAIL_OUTBOX_ENCRYPTION_KEY
    if not raw:
        raise ValueError("EMAIL_OUTBOX_ENCRYPTION_KEY 未配置（须为 b64url 32 字节密钥）")
    return make_keyring(f"primary:{raw}")


def _worker_id() -> str:
    """租约持有者标识（lease_owner 列 String(100)）：进程级唯一即可区分并发实例。"""
    return f"outbox-{os.getpid()}"


async def enqueue(
    db: AsyncSession,
    *,
    purpose: str,
    user_id: uuid.UUID,
    recipient: str,
    action_token: str,
    outbox_id: uuid.UUID | None = None,
) -> None:
    """在调用方 owner 事务内投递一封待发邮件（**不提交**——事务归属调用方）。

    生成 oid（或采用调用方指定的 outbox_id）；payload 经白名单构造 + 信封加密
    （AAD 绑定 oid）后落 email_outbox 行（state=pending，attempts 由 ORM default=0）。
    flush 即时在调用方事务内执行 INSERT——RLS/约束违例在此暴露，不拖到 commit；
    调用方回滚时 outbox 行一并回滚（transactional outbox 原子性）。
    """
    oid = outbox_id or uuid7()
    if not isinstance(oid, uuid.UUID):
        oid = uuid.UUID(str(oid))
    owner_id = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    payload = _build_payload(purpose, recipient, action_token)
    active_kid, keyring = _outbox_keyring()
    envelope = encrypt_text(
        json.dumps(payload, ensure_ascii=False),
        aad=_outbox_aad(oid),
        keyring=keyring,
        active_kid=active_kid,
    )
    db.add(
        EmailOutbox(
            id=oid,
            user_id=owner_id,
            purpose=purpose,
            payload_ciphertext=json.dumps(envelope),
            state="pending",
        )
    )
    await db.flush()


# 认领：单条 UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING。
# 谓词：state=pending 且（无重试时间或已到期）且（无租约或租约已过期——崩溃恢复）。
# ORDER BY created_at 保证 FIFO；SKIP LOCKED 保证多实例并发不双认领。
_CLAIM_SQL = text(
    "UPDATE email_outbox SET "
    "    lease_owner = :worker, "
    "    lease_expires_at = now() + make_interval(mins => :lease_minutes) "
    "WHERE id IN ("
    "    SELECT id FROM email_outbox "
    "    WHERE state = 'pending' "
    "      AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
    "      AND (lease_expires_at IS NULL OR lease_expires_at < now()) "
    "    ORDER BY created_at "
    "    LIMIT :batch "
    "    FOR UPDATE SKIP LOCKED"
    ") "
    "RETURNING id, purpose, payload_ciphertext, attempts"
)

# 终态写入统一附 lease_owner 谓词（乐观护栏）：租约被其他 worker 接管后本 worker
# 的写入静默 0 行，不计入 sent 返回值（该行由接管者重放，at-least-once 语义）。
_SENT_SQL = text(
    "UPDATE email_outbox SET state = 'sent', lease_owner = NULL, lease_expires_at = NULL "
    "WHERE id = :id AND lease_owner = :worker"
)
_RETRY_SQL = text(
    "UPDATE email_outbox SET lease_owner = NULL, lease_expires_at = NULL, "
    "attempts = :attempts, next_attempt_at = now() + make_interval(secs => :secs) "
    "WHERE id = :id AND lease_owner = :worker"
)
_DEAD_SQL = text(
    "UPDATE email_outbox SET state = 'delivery_failed', lease_owner = NULL, "
    "lease_expires_at = NULL, attempts = :attempts "
    "WHERE id = :id AND lease_owner = :worker"
)


async def dispatch_once(runtime: V2Runtime, transport, *, batch: int = 10) -> int:
    """认领一批到期 pending 行并逐行投递；返回**本轮转为 'sent' 的行数**。

    （精确定义：成功终态 UPDATE rowcount=1 的行数；解密/发送失败与丢租约的行不计。）

    admin role 两段式：
    1. 认领事务：租约打标（lease_owner=worker，5 分钟）并立即提交；
    2. 终态事务（逐行）：事务外解密 + transport.send → sent / 重试退避 / delivery_failed；
       解密失败与 transport 异常等同计为投递失败（attempts+1；达 MAX_ATTEMPTS 终态
       并记 ERROR）。
    """
    if batch < 1:
        raise ValueError("batch 必须 >= 1")
    worker = _worker_id()
    _, keyring = _outbox_keyring()  # 解密按信封 kid 取钥，无需写入侧 active kid

    async with runtime.admin_factory() as session:
        async with session.begin():
            claimed = (
                (
                    await session.execute(
                        _CLAIM_SQL,
                        {"worker": worker, "lease_minutes": LEASE_MINUTES, "batch": batch},
                    )
                )
                .mappings()
                .all()
            )

    sent_count = 0
    for row in claimed:
        if await _finalize_row(runtime, transport, row, worker, keyring):
            sent_count += 1
    return sent_count


async def _finalize_row(runtime: V2Runtime, transport, row, worker: str, keyring) -> bool:
    """单行终态（独立短事务）：解密 → 投递 → sent/退避/delivery_failed。返回是否 sent。"""
    oid = row["id"]
    purpose = row["purpose"]
    ok, error = True, None
    try:
        envelope = json.loads(row["payload_ciphertext"])
        plaintext = decrypt_text(envelope, aad=_outbox_aad(oid), keyring=keyring)
        payload = json.loads(plaintext)
        await transport.send(purpose=purpose, payload=payload)
    except Exception as exc:  # 单行投递失败必须被隔离为 attempts+1，不拖垮整批
        ok, error = False, exc

    new_attempts = int(row["attempts"]) + 1
    final_fail = not ok and new_attempts >= MAX_ATTEMPTS
    async with runtime.admin_factory() as session:
        async with session.begin():
            if ok:
                result = await session.execute(_SENT_SQL, {"id": oid, "worker": worker})
            elif final_fail:
                result = await session.execute(
                    _DEAD_SQL, {"id": oid, "attempts": new_attempts, "worker": worker}
                )
            else:
                result = await session.execute(
                    _RETRY_SQL,
                    {
                        "id": oid,
                        "attempts": new_attempts,
                        "secs": BACKOFF_SECONDS[new_attempts - 1],
                        "worker": worker,
                    },
                )
            rowcount = result.rowcount

    if rowcount == 1 and final_fail:
        # 管理员告警语义：仅含元数据（id/purpose/attempts），无 payload 内容
        logger.error(
            "outbox delivery failed: id=%s purpose=%s attempts=%d",
            oid,
            purpose,
            new_attempts,
        )
    elif rowcount == 1 and not ok:
        logger.warning(
            "outbox delivery attempt failed: id=%s purpose=%s attempts=%d error=%s",
            oid,
            purpose,
            new_attempts,
            type(error).__name__,
        )
    return ok and rowcount == 1


async def outbox_loop(runtime: V2Runtime, transport, poll_seconds: float = 5) -> None:
    """常驻派发循环（lifespan 后台协程）：每轮 dispatch_once 后睡 poll_seconds。

    单轮异常只记录不外抛（DB 抖动/密钥轮换窗口等不得杀死进程）；仅 CancelledError
    穿透供 lifespan 关停取消。首轮立即派发，其后按 poll_seconds 轮询。
    """
    while True:
        try:
            sent = await dispatch_once(runtime, transport)
            if sent:
                logger.info("outbox dispatch cycle sent=%d", sent)
        except Exception:
            logger.exception("outbox dispatch cycle failed; will retry")
        await asyncio.sleep(poll_seconds)
