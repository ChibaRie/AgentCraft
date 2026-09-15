"""admin 面共享门依赖与壳原语（Phase 7 T1，裁决 D2/D6/D11）。

- ``get_v2_admin_auth``（D2 三重门，全部读 ctx 字段零新查询）：
  ①role 门（非 admin → 403 FORBIDDEN）→ ②TOTP 兜底（mfa_secret_enc IS NULL →
  403 ADMIN_MFA_REQUIRED——未配置者永不过门）→ ③12h 时效（mfa_verified_at IS NULL
  或 now - mfa_verified_at > 12h → 403 ADMIN_MFA_REQUIRED）。②③统一文案不区分
  失败因子（不泄漏「已配置但过期」与「从未配置」的差异）。读写全受门（Sup:126）；
  续期 = 重登录（step-up 登记缺口归 Phase 8）。门序（D6 钉死）：CSRF → 认证
  （get_v2_auth）→ admin 三重门 → 幂等 begin → 限流（仅 kill-switch）→ 状态门 → 服务。
- ``require_admin_reason``：壳层独立实现（服务层无 ≤2000 门，不混用——D11）。
  None/空白 → 400 ADMIN_REASON_REQUIRED；>2000 字符 → 400 VALIDATION_ERROR。
- admin 幂等三段壳助手（T8a reports/tasks 壳同构移植；tasks.py _idem_begin/
  _idem_store 同型）：``admin_idem_begin`` 用 app 裸会话（own_transaction——命中
  重放与未命中的机会主义过期行清理随自持事务 COMMIT 落库）→ 调用方 admin 事务
  （runtime.admin_factory().begin()）内跑服务 + ``admin_idem_store`` → 提交。
  subject=subject_user(admin_id)（owner 主体派生 = admin 维度）；route=具体请求
  路径（含资源 ID，不做模板归并）；store 不 commit 随调用方 admin 事务收口。
"""

from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2 import idempotency
from backend.v2.idempotency import subject_user
from backend.v2.runtime import V2Runtime
from backend.v2.session_service import V2AuthContext, get_v2_auth

# D2③：admin MFA 验证时效（mfa_verified_at 距今超过 12h 即失效；续期 = 重登录）
_MFA_WINDOW = timedelta(hours=12)
_REASON_MAX_LENGTH = 2000

# 统一失败文案（契约钉死；FORBIDDEN 字面与 login A9 逐字节一致——T1 断言钉）
_FORBIDDEN_DETAIL = {"code": ErrorCode.FORBIDDEN.value, "message": "需要管理员权限"}
_ADMIN_MFA_REQUIRED_DETAIL = {
    "code": ErrorCode.ADMIN_MFA_REQUIRED.value,
    "message": "管理员操作需要有效的 MFA 验证",
}


async def get_v2_admin_auth(ctx: V2AuthContext = Depends(get_v2_auth)) -> V2AuthContext:
    """admin 三重门（D2）：挂在 get_v2_auth 之后的依赖——全读 ctx 零新查询。

    ①role 门（非 admin 403 FORBIDDEN）→ ②TOTP 兜底（mfa_secret_enc NULL 403
    ADMIN_MFA_REQUIRED，未配置者永不过门）→ ③12h 时效（mfa_verified_at NULL 或
    超 12h 403 ADMIN_MFA_REQUIRED）。通过则原样返回 ctx（零拷贝，无副作用）。
    """
    if ctx.user.role != "admin":
        raise HTTPException(status_code=403, detail=_FORBIDDEN_DETAIL)
    if ctx.user.mfa_secret_enc is None:
        raise HTTPException(status_code=403, detail=_ADMIN_MFA_REQUIRED_DETAIL)
    verified_at = ctx.session.mfa_verified_at
    if verified_at is None or datetime.now(timezone.utc) - verified_at > _MFA_WINDOW:
        raise HTTPException(status_code=403, detail=_ADMIN_MFA_REQUIRED_DETAIL)
    return ctx


def require_admin_reason(payload_reason: str | None) -> str:
    """admin 写端点操作理由门：None/空白 → 400 ADMIN_REASON_REQUIRED；>2000 → 400
    VALIDATION_ERROR。原样返回入参（归一化不在此做——原样参与 request_hash）。"""
    if payload_reason is None or not payload_reason.strip():
        raise AgentCraftError(
            ErrorCode.ADMIN_REASON_REQUIRED, "缺少 admin 操作理由（reason）", http_status=400
        )
    if len(payload_reason) > _REASON_MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": f"reason 过长（最多 {_REASON_MAX_LENGTH} 字符）",
            },
        )
    return payload_reason


async def admin_idem_begin(
    runtime: V2Runtime, *, request: Request, admin_id: str, key: str, req_hash: str
) -> JSONResponse | None:
    """段一（app 裸会话，提交式自持事务）：幂等查询。

    命中且 request_hash 一致 → 原样重放 JSONResponse（幂等命中优先于一切状态
    检查，无条件下重放）；未命中/已过期 → None（机会主义过期行清理随自持事务
    COMMIT 落库）；命中但 request_hash 不一致 → idempotency.begin 抛 409
    IDEMPOTENCY_CONFLICT（reason 原样参与哈希：同 key 异 reason 即冲突）。
    """
    async with runtime.app_factory() as db:
        hit = await idempotency.begin(
            db,
            subject_hash=subject_user(admin_id),
            route=request.url.path,
            key=key,
            req_hash=req_hash,
        )
    if hit is None:
        return None
    return JSONResponse(status_code=hit["status_code"], content=hit["response_json"])


async def admin_idem_store(
    db: AsyncSession,
    *,
    request: Request,
    admin_id: str,
    key: str,
    req_hash: str,
    status_code: int,
    payload: dict,
) -> None:
    """段二（调用方 admin 事务内）：幂等记录落库，**不 commit**——随 admin 事务
    与业务写同事务收口（提交 / 异常一并回滚）。"""
    await idempotency.store(
        db,
        subject_hash=subject_user(admin_id),
        route=request.url.path,
        key=key,
        req_hash=req_hash,
        status_code=status_code,
        response_json=payload,
    )
