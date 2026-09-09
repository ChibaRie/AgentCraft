"""幂等引擎（API Supplement §7）：写端点的同 key 重放/冲突裁决与凭据脱敏。

调用契约（调用方必读）：
- ``begin`` 命中且 request_hash 一致 → 返回 ``{"status_code", "response_json"}``，
  调用方**原样重放，无论当前业务状态**（幂等命中优先于一切状态检查）；
- ``begin`` 命中但 request_hash 不一致 → ``AgentCraftError(IDEMPOTENCY_CONFLICT,
  409)``；未命中或已过期 → None（并在同事务机会主义 DELETE 全部过期行）；
- ``store`` 必须在调用方业务事务内调用，本函数**不 commit**；并发同
  (subject_hash, route, key) 的失败方在 flush 时命中唯一索引
  ``idempotency_route_key``，此处将 IntegrityError 转为
  ``AgentCraftError(IDEMPOTENCY_CONFLICT, 409)`` 抛出——**回滚是调用方的职责**
  （典型形态：owner_session 的 begin() 块随异常自动回滚；调用方捕获后重查重放）；
- ``expires_at`` 以应用时钟（UTC）+24h 写入，过期判定与数据库 ``now()`` 比较
  （24h 为重放窗口量级，应用/DB 时钟毫秒级偏差不构成正确性边界）。
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.models.tasking import IdempotencyRecord
from backend.v2.security import hash_token

CREDENTIAL_FIELDS: frozenset[str] = frozenset(
    {
        "password",
        "new_password",
        "invitation_token",
        "verify_token",
        "reset_token",
        "cancel_token",
        "api_key",
        "totp_code",
    }
)
"""request_hash 脱敏字段（仅顶层键）：值以固定占位符参与哈希，凭据差异不构成冲突。"""

REDACTED_PLACEHOLDER = "***IDEMPOTENCY-REDACTED***"

_RECORD_TTL = timedelta(hours=24)  # 重放窗口（§7）
_KEY_MAX_LENGTH = 100  # key 列 VARCHAR(100)，入库前拒绝防 22001 溢出变 500


def request_hash(payload: dict | None) -> str:
    """规范化 JSON（键排序、紧凑分隔符、ensure_ascii=False）后 SHA-256。

    顶层 CREDENTIAL_FIELDS 键以固定占位符参与哈希；payload 为 None 时按
    规范化 ``"null"`` 参与哈希。嵌套结构内的同名键不脱敏（仅顶层）。
    """
    body = None
    if payload is not None:
        body = {
            k: REDACTED_PLACEHOLDER if k in CREDENTIAL_FIELDS else v for k, v in payload.items()
        }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def subject_user(user_id: object) -> str:
    """owner 主体派生：幂等作用域 = 用户维度。"""
    return hash_token(str(user_id))


def subject_token(token: str) -> str:
    """预认证端点主体派生（§7）：action token 哈希——匿名请求无用户上下文。"""
    return hash_token(token)


async def begin(
    db: AsyncSession, *, subject_hash: str, route: str, key: str, req_hash: str
) -> dict | None:
    """查询幂等记录：命中一致 → 重放载荷；命中冲突 → 409；未命中/过期 → None。"""
    row = (
        await db.execute(
            select(
                IdempotencyRecord.status_code,
                IdempotencyRecord.response_json,
                IdempotencyRecord.request_hash,
            ).where(
                IdempotencyRecord.subject_hash == subject_hash,
                IdempotencyRecord.route == route,
                IdempotencyRecord.key == key,
                IdempotencyRecord.expires_at > func.now(),
            )
        )
    ).first()
    if row is None:
        # 机会主义清理：同事务删除全部过期行（含本次未命中的那条，若有），
        # 使调用方随后的 store 不会撞上残留过期行的唯一索引
        await db.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.expires_at <= func.now())
        )
        return None
    if row.request_hash != req_hash:
        raise AgentCraftError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "请求体与已记录的幂等请求不一致",
            http_status=409,
        )
    return {"status_code": row.status_code, "response_json": row.response_json}


async def store(
    db: AsyncSession,
    *,
    subject_hash: str,
    route: str,
    key: str,
    req_hash: str,
    status_code: int,
    response_json: dict,
) -> None:
    """在调用方业务事务内写入幂等记录（expires_at = now() + 24h；不 commit）。"""
    record = IdempotencyRecord(
        subject_hash=subject_hash,
        route=route,
        key=key,
        request_hash=req_hash,
        status_code=status_code,
        response_json=response_json,
        expires_at=datetime.now(timezone.utc) + _RECORD_TTL,
    )
    db.add(record)
    try:
        # 显式 flush：唯一索引冲突（并发同 key 的失败方）在此浮出，
        # 而不是等到调用方 commit 才以裸 IntegrityError 形态出现
        await db.flush()
    except IntegrityError as exc:
        raise AgentCraftError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "并发幂等请求冲突，请回滚后重查重放",
            http_status=409,
        ) from exc


def require_key_header(request: Request) -> str:
    """FastAPI 依赖：提取并校验 ``Idempotency-Key`` 头（≤100 字符）。"""
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "缺少 Idempotency-Key 头"},
        )
    if len(key) > _KEY_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "Idempotency-Key 过长（最多 100 字符）",
            },
        )
    return key
