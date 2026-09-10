"""V2 认证面路由。

Task 4 占位 ``GET /health``（503 语义探针）；Task 9 增加
``POST /auth/invitations/accept``（挂载于 /api/v2，公开端点：无会话无 CSRF，
Idempotency-Key 必带）。依赖统一定义于 backend.v2.runtime / idempotency /
session_service，此处仅消费。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from backend.api.v2.schemas import InvitationAcceptRequest
from backend.v2 import idempotency, invitation_service
from backend.v2.idempotency import require_key_header
from backend.v2.runtime import V2Runtime, client_ip, get_v2_runtime
from backend.v2.security import device_label_from_ua
from backend.v2.session_service import set_csrf_cookie, set_session_cookie

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
