"""V2 账户面路由。

Task 4 空壳；Task 13 增加 ``POST /account/deletion/request``（认证端点：
get_v2_auth 门序 + CSRF + Idempotency-Key 必带）、``POST /account/deletion/cancel``
（公开端点：无会话无 CSRF，Idempotency-Key 必带）与 ``GET /account/deletion/status``
（认证端点）；Task 14 增加 ``GET /users/me``（认证端点：本人资料 + 生效
entitlements + mfa_enabled，不返回 csrf_token——A14 交付信道钉死）。
依赖统一定义于 backend.v2.runtime / idempotency / session_service，此处仅消费。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from backend.api.v2.schemas import DeletionCancelRequest, DeletionRequestRequest
from backend.v2 import deletion_service, idempotency
from backend.v2.idempotency import require_key_header
from backend.v2.runtime import V2Runtime, client_ip, get_v2_runtime, owner_session
from backend.v2.security import device_label_from_ua
from backend.v2.session_service import (
    V2AuthContext,
    clear_session_cookie,
    get_v2_auth,
    set_csrf_cookie,
    set_session_cookie,
)

router = APIRouter()


@router.post("/account/deletion/request")
async def request_account_deletion(
    payload: DeletionRequestRequest,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """发起账户注销（认证端点）：再认证（密码必验 + TOTP 叠加）→ 14 天宽限期。

    Idempotency-Key 必带（A5，subject=user）：命中 → 存量响应原样重放（刻意不
    携带 Set-Cookie）。新成功路径清除双 cookie（A10：全会话失效，含当前）。
    """
    outcome = await deletion_service.request_deletion(
        runtime,
        user_id=str(user_ctx.user.id),
        status=user_ctx.user.status,
        password_hash=user_ctx.user.password_hash,
        mfa_secret_enc=user_ctx.user.mfa_secret_enc,
        email=user_ctx.user.email,
        password=payload.password,
        totp_code=payload.totp_code,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(payload.model_dump()),
    )
    if isinstance(outcome, deletion_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    response = JSONResponse(status_code=200, content=outcome)
    clear_session_cookie(response)
    return response


@router.post("/account/deletion/cancel")
async def cancel_account_deletion(
    payload: DeletionCancelRequest,
    request: Request,
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """撤销注销（公开端点，凭邮件中的 cancel_token）：恢复 active + 全新会话。

    无会话无 CSRF；Idempotency-Key 必带（A5，subject=token）：命中 → 存量响应
    原样重放（刻意不携带 Set-Cookie，不重复建会话 A12）。新成功路径种双 cookie。
    """
    outcome = await deletion_service.cancel_deletion(
        runtime,
        cancel_token=payload.cancel_token,
        password=payload.password,
        ip=client_ip(request),
        device_label=device_label_from_ua(request.headers.get("user-agent")),
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(payload.model_dump()),
    )
    if isinstance(outcome, deletion_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    response = JSONResponse(status_code=200, content=outcome.body)
    set_session_cookie(response, outcome.session_token)
    set_csrf_cookie(response, outcome.body["data"]["csrf_token"])
    return response


@router.get("/account/deletion/status")
async def get_account_deletion_status(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
) -> JSONResponse:
    """注销状态查询（认证端点）：deleting 才有 days_remaining/deadline_at。

    A10 语义：deleting 用户全会话已失效，实际经 HTTP 不可达本端点（401）；本端点
    服务于 active/pending 用户的 null 形状与后续阶段的服务层复用。
    """
    body = deletion_service.deletion_status_view(
        status=user_ctx.user.status, deadline_at=user_ctx.user.deletion_deadline_at
    )
    return JSONResponse(status_code=200, content={"data": body})


@router.get("/users/me")
async def get_users_me(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """当前用户资料（认证端点，GET 免 CSRF）：owner 事务读本人行 + 生效 entitlements。

    entitlements = user_entitlements WHERE revoked_at IS NULL（0003 后 app 可读本人
    行）；mfa_enabled = mfa_secret_enc IS NOT NULL。**不返回 csrf_token**（A14：
    服务端仅存 csrf_hash，明文交付信道 = 会话创建类响应 body + ac_csrf 镜像 cookie）。
    """
    user_id = str(user_ctx.user.id)
    async with owner_session(runtime, user_id) as db:
        row = (
            await db.execute(
                text("SELECT email, role, status, mfa_secret_enc FROM users WHERE id = :u"),
                {"u": user_id},
            )
        ).one()
        entitlements = (
            (
                await db.execute(
                    text(
                        "SELECT entitlement FROM user_entitlements "
                        "WHERE user_id = :u AND revoked_at IS NULL ORDER BY entitlement"
                    ),
                    {"u": user_id},
                )
            )
            .scalars()
            .all()
        )
    return JSONResponse(
        status_code=200,
        content={
            "data": {
                "id": user_id,
                "email": row.email,
                "role": row.role,
                "status": row.status,
                "entitlements": list(entitlements),
                "mfa_enabled": row.mfa_secret_enc is not None,
            }
        },
    )
