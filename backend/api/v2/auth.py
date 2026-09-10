"""V2 认证面路由。

Task 4 占位 ``GET /health``（503 语义探针）；Task 9 增加
``POST /auth/invitations/accept``（挂载于 /api/v2，公开端点：无会话无 CSRF，
Idempotency-Key 必带）；Task 10 增加 ``POST /auth/email-verification/confirm``
（公开端点：无会话无 CSRF，Idempotency-Key 必带，无限流）与
``POST /auth/email-verification/resend``（认证端点：get_v2_auth 门序 + 限流）；
Task 11 增加 ``POST /auth/login`` 与 ``POST /auth/login/mfa``（公开端点：无会话
无 CSRF，A5 未列入幂等键控）以及 ``POST /auth/mfa/setup`` /
``POST /auth/mfa/activate`` / ``DELETE /auth/mfa``（认证端点：get_v2_auth 门序）；
Task 12 增加 ``POST /auth/password-reset/request``（公开端点：无 CSRF，A5 未列入
幂等键控，恒 202）与 ``POST /auth/password-reset/confirm``（公开端点：
Idempotency-Key 必带）以及 ``POST /auth/password-change``（认证端点：A7 契约缺口
补端点）；Task 14 增加 ``POST /auth/logout``（认证端点：豁免幂等 A5，撤销当前
会话 + 清双 cookie）、``GET /auth/sessions``（认证端点：设备会话列表信封）与
``DELETE /auth/sessions/{session_id}``（认证端点：Idempotency-Key 必带 A5，
subject=user、route=含资源 ID 的具体路径；rowcount 0 统一 404，绝不 403）。
依赖统一定义于 backend.v2.runtime / idempotency / session_service，此处仅消费。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from backend.api.v2.schemas import (
    EmailVerificationConfirmRequest,
    InvitationAcceptRequest,
    LoginRequest,
    MfaActivateRequest,
    MfaChallengeRequest,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequestRequest,
)
from backend.v2 import (
    idempotency,
    invitation_service,
    login_service,
    password_service,
    verification_service,
)
from backend.v2.idempotency import require_key_header
from backend.v2.runtime import V2Runtime, client_ip, get_v2_runtime, owner_session
from backend.v2.security import device_label_from_ua
from backend.v2.session_service import (
    V2AuthContext,
    clear_session_cookie,
    get_v2_auth,
    list_sessions,
    revoke,
    set_csrf_cookie,
    set_session_cookie,
)

# 恒 202 防枚举载荷（request 端点固定出口；服务层不发信形态亦同形）
_RESET_ACCEPTED_BODY = {"data": {"accepted": True}}

# 统一 404 文案（契约钉死；V1 惯例 HTTPException——NOT_FOUND 非注册表错误码）
_NOT_FOUND_DETAIL = {"code": "NOT_FOUND", "message": "资源不存在"}

router = APIRouter()


def _session_response(body: dict, session_token: str) -> JSONResponse:
    """登录成功统一出口：200 响应体 + 会话/CSRF 双 cookie（A14）。"""
    response = JSONResponse(status_code=200, content=body)
    set_session_cookie(response, session_token)
    set_csrf_cookie(response, body["data"]["csrf_token"])
    return response


@router.get("/health")
async def v2_health(_runtime: V2Runtime = Depends(get_v2_runtime)) -> dict[str, object]:
    return {"data": {"status": "ok"}}


@router.post("/auth/invitations/accept")
async def accept_invitation(
    payload: InvitationAcceptRequest,
    request: Request,
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """接受邀请（公开端点）：落建 pending 用户、种会话双 cookie、验证邮件入队。

    Idempotency-Key 必带（A5）：命中 → 存量响应原样重放（刻意不携带 Set-Cookie）；
    新成功路径才种双 cookie（cookie 明文刻意不入幂等重放载荷）。
    请求体哈希以 ``model_dump(exclude_unset=True)`` 供给（凭据三态标记见 D12）。
    """
    outcome = await invitation_service.accept_invitation(
        runtime,
        invitation_token=payload.invitation_token,
        email=payload.email,
        password=payload.password,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(payload.model_dump(exclude_unset=True)),
        ip=client_ip(request),
        device_label=device_label_from_ua(request.headers.get("user-agent")),
    )
    if isinstance(outcome, invitation_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return _session_response(outcome.body, outcome.session_token)


@router.post("/auth/email-verification/confirm")
async def confirm_email_verification(
    payload: EmailVerificationConfirmRequest,
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """邮箱验证确认（公开端点）：令牌单次消费 + pending→active 跃迁。

    无会话无 CSRF；无限流（服务 docstring 注明滥用面由令牌单次消费约束）。
    Idempotency-Key 必带（A5）：命中 → 存量响应原样重放，无论令牌当前状态（§7）。
    请求体哈希以 ``model_dump(exclude_unset=True)`` 供给（凭据三态标记见 D12）。
    """
    outcome = await verification_service.confirm_email_verification(
        runtime,
        verify_token=payload.verify_token,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(payload.model_dump(exclude_unset=True)),
    )
    if isinstance(outcome, verification_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content=outcome)


@router.post("/auth/email-verification/resend")
async def resend_email_verification(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """验证邮件重发（认证端点）：仅 pending 用户；旧令牌作废 + 新令牌 + outbox 同事务。

    认证走 get_v2_auth 门序（会话 cookie → CSRF → 状态门）；限流在服务层（enforce
    自管事务独立提交）。令牌仅经邮件投递，不入响应体。
    """
    await verification_service.resend_email_verification(
        runtime,
        user_id=str(user_ctx.user.id),
        recipient=user_ctx.user.email,
        status=user_ctx.user.status,
    )
    return JSONResponse(status_code=200, content={"data": {"sent": True}})


@router.post("/auth/login")
async def login(
    payload: LoginRequest,
    request: Request,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """登录（公开端点）：无会话无 CSRF，A5 未列入幂等键控。

    限流/防枚举/状态门在服务层收口。TOTP 已启用 → 200 mfa_required + 挑战 id
    （不建会话不种 cookie）；常规路径 → 双 cookie + user/csrf 响应体（A14）。
    """
    outcome = await login_service.login(
        runtime,
        email=payload.email,
        password=payload.password,
        ip=client_ip(request),
        device_label=device_label_from_ua(request.headers.get("user-agent")),
    )
    if isinstance(outcome, login_service.MfaRequired):
        return JSONResponse(
            status_code=200,
            content={"data": {"mfa_required": True, "mfa_challenge_id": outcome.mfa_challenge_id}},
        )
    return _session_response(outcome.body, outcome.session_token)


@router.post("/auth/login/mfa")
async def login_mfa(
    payload: MfaChallengeRequest,
    request: Request,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """MFA 挑战验证（公开端点）：挑战一次性消费；成功建 mfa_verified 会话。

    挑战未知/过期/码错统一 401 MFA_INVALID；成功 → 双 cookie + user/csrf 响应体。
    """
    outcome = await login_service.login_mfa(
        runtime,
        mfa_challenge_id=payload.mfa_challenge_id,
        totp_code=payload.totp_code,
        device_label=device_label_from_ua(request.headers.get("user-agent")),
    )
    return _session_response(outcome.body, outcome.session_token)


@router.post("/auth/logout")
async def logout(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """登出（认证端点，豁免幂等 A5）：撤销当前会话 + 双 cookie 清除。

    认证走 get_v2_auth 门序（会话 cookie → CSRF → 状态门）；owner 事务内软撤销
    （行保留供设备列表展示）。重放同一死 cookie 自然 401 SESSION_EXPIRED（会话
    已撤销，无特殊处理）。
    """
    async with owner_session(runtime, str(user_ctx.user.id)) as db:
        await revoke(db, user_ctx.session.id)
    response = JSONResponse(status_code=200, content={"data": {"ok": True}})
    clear_session_cookie(response)
    return response


@router.get("/auth/sessions")
async def list_device_sessions(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """设备会话列表（认证端点，GET 免 CSRF）：{data, total, page, size} 列表信封。

    owner 事务内 list_sessions（RLS 限本行）：{id, device_label, created_at,
    expires_at, current}；时间戳 ISO 序列化；current = 当前请求会话标志。
    """
    async with owner_session(runtime, str(user_ctx.user.id)) as db:
        rows = await list_sessions(db, user_ctx.session.id)
    items = [
        {
            "id": str(row["id"]),
            "device_label": row["device_label"],
            "created_at": row["created_at"].isoformat(),
            "expires_at": row["expires_at"].isoformat(),
            "current": row["current"],
        }
        for row in rows
    ]
    return JSONResponse(
        status_code=200,
        content={"data": items, "total": len(items), "page": 1, "size": len(items)},
    )


@router.delete("/auth/sessions/{session_id}")
async def delete_device_session(
    session_id: uuid.UUID,
    request: Request,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """删除设备会话（认证端点，A5：Idempotency-Key 必带，subject=user）。

    幂等 begin 最先（§7 重放优先：同 key 已成功删除的重放返回原 200，无论会话
    当前状态；route = 含资源 ID 的具体路径，重放不携带 Set-Cookie）→ owner 事务
    revoke：rowcount 0 → 统一 404「资源不存在」（他人会话/不存在/已撤销一律 404，
    owner-RLS 保证绝不 403 泄漏存在性，随事务回滚分文不写）；rowcount 1 → 幂等
    store 同事务；撤销当前会话时同时清双 cookie。
    """
    subject = idempotency.subject_user(str(user_ctx.user.id))
    route = request.url.path
    req_hash = idempotency.request_hash(None)  # DELETE 无请求体
    async with runtime.app_factory() as db:
        replay = await idempotency.begin(
            db, subject_hash=subject, route=route, key=idem_key, req_hash=req_hash
        )
    if replay is not None:
        return JSONResponse(status_code=replay["status_code"], content=replay["response_json"])

    async with owner_session(runtime, str(user_ctx.user.id)) as db:
        if await revoke(db, session_id) == 0:
            raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)
        body = {"data": {"ok": True}}
        await idempotency.store(
            db,
            subject_hash=subject,
            route=route,
            key=idem_key,
            req_hash=req_hash,
            status_code=200,
            response_json=body,
        )
    response = JSONResponse(status_code=200, content=body)
    if session_id == user_ctx.session.id:
        clear_session_cookie(response)
    return response


@router.post("/auth/mfa/setup")
async def setup_mfa(user_ctx: V2AuthContext = Depends(get_v2_auth)) -> JSONResponse:
    """TOTP 注册第一步（认证端点）：生成 secret（内存 pending，10 分钟 TTL）。

    secret 与 otpauth URI 入响应体交由用户录入验证器 App；不落库（activate 才写）。
    """
    body = login_service.setup_mfa(user_id=str(user_ctx.user.id), email=user_ctx.user.email)
    return JSONResponse(status_code=200, content=body)


@router.post("/auth/mfa/activate")
async def activate_mfa(
    payload: MfaActivateRequest,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """TOTP 注册第二步（认证端点）：验证码正确 → 信封加密落库 + 当前会话盖 MFA 戳。"""
    body = await login_service.activate_mfa(
        runtime,
        user_id=str(user_ctx.user.id),
        session_id=str(user_ctx.session.id),
        totp_code=payload.totp_code,
    )
    return JSONResponse(status_code=200, content=body)


@router.delete("/auth/mfa")
async def disable_mfa(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """停用 TOTP（认证端点）：admin 不可停用（A9）；其他用户清 mfa_secret_enc。"""
    body = await login_service.disable_mfa(
        runtime, user_id=str(user_ctx.user.id), role=user_ctx.user.role
    )
    return JSONResponse(status_code=200, content=body)


@router.post("/auth/password-reset/request")
async def request_password_reset(
    payload: PasswordResetRequestRequest,
    request: Request,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """密码重置请求（公开端点）：无会话无 CSRF，A5 未列入幂等键控。

    恒 202 防枚举（服务层不发信形态对响应不可见）；限流在服务层收口（先于任何
    用户查询）。令牌仅经邮件投递，不入响应体。
    """
    await password_service.request_password_reset(
        runtime, email=payload.email, ip=client_ip(request)
    )
    return JSONResponse(status_code=202, content=_RESET_ACCEPTED_BODY)


@router.post("/auth/password-reset/confirm")
async def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """密码重置确认（公开端点）：令牌单次消费 + 全部会话失效（重新登录强制）。

    无会话无 CSRF；无限流（服务 docstring 注明滥用面由令牌单次消费约束，A10）。
    Idempotency-Key 必带（A5）：命中 → 存量响应原样重放，无论令牌当前状态（§7）。
    请求体哈希以 ``model_dump(exclude_unset=True)`` 供给（凭据三态标记见 D12）。
    """
    outcome = await password_service.confirm_password_reset(
        runtime,
        reset_token=payload.reset_token,
        new_password=payload.new_password,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(payload.model_dump(exclude_unset=True)),
    )
    if isinstance(outcome, password_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content=outcome)


@router.post("/auth/password-change")
async def change_password(
    payload: PasswordChangeRequest,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """密码修改（认证端点，A7 契约缺口补端点）：TOTP 门（A15 先于 Argon2）→
    当前密码验证 → 更新哈希 + 撤销其余会话（保留当前）。

    认证走 get_v2_auth 门序（会话 cookie → CSRF → 状态门）；用户凭据列取自
    认证上下文（同请求内 fresh 解析），业务在服务层收口。
    """
    body = await password_service.change_password(
        runtime,
        user_id=str(user_ctx.user.id),
        session_id=str(user_ctx.session.id),
        current_password_hash=user_ctx.user.password_hash,
        mfa_secret_enc=user_ctx.user.mfa_secret_enc,
        current_password=payload.current_password,
        new_password=payload.new_password,
        totp_code=payload.totp_code,
    )
    return JSONResponse(status_code=200, content=body)
