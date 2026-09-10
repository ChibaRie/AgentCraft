"""V2 认证面路由。

Task 4 占位 ``GET /health``（503 语义探针）；Task 9 增加
``POST /auth/invitations/accept``（挂载于 /api/v2，公开端点：无会话无 CSRF，
Idempotency-Key 必带）；Task 10 增加 ``POST /auth/email-verification/confirm``
（公开端点：无会话无 CSRF，Idempotency-Key 必带，无限流）与
``POST /auth/email-verification/resend``（认证端点：get_v2_auth 门序 + 限流）。
依赖统一定义于 backend.v2.runtime / idempotency / session_service，此处仅消费。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from backend.api.v2.schemas import EmailVerificationConfirmRequest, InvitationAcceptRequest
from backend.v2 import idempotency, invitation_service, verification_service
from backend.v2.idempotency import require_key_header
from backend.v2.runtime import V2Runtime, client_ip, get_v2_runtime
from backend.v2.security import device_label_from_ua
from backend.v2.session_service import (
    V2AuthContext,
    get_v2_auth,
    set_csrf_cookie,
    set_session_cookie,
)

router = APIRouter()


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
    """
    outcome = await invitation_service.accept_invitation(
        runtime,
        invitation_token=payload.invitation_token,
        email=payload.email,
        password=payload.password,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(payload.model_dump()),
        ip=client_ip(request),
        device_label=device_label_from_ua(request.headers.get("user-agent")),
    )
    if isinstance(outcome, invitation_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    response = JSONResponse(status_code=200, content=outcome.body)
    set_session_cookie(response, outcome.session_token)
    set_csrf_cookie(response, outcome.body["data"]["csrf_token"])
    return response


@router.post("/auth/email-verification/confirm")
async def confirm_email_verification(
    payload: EmailVerificationConfirmRequest,
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """邮箱验证确认（公开端点）：令牌单次消费 + pending→active 跃迁。

    无会话无 CSRF；无限流（服务 docstring 注明滥用面由令牌单次消费约束）。
    Idempotency-Key 必带（A5）：命中 → 存量响应原样重放，无论令牌当前状态（§7）。
    """
    outcome = await verification_service.confirm_email_verification(
        runtime,
        verify_token=payload.verify_token,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(payload.model_dump()),
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
