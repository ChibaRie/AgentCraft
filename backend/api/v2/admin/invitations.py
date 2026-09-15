"""admin 邀请管理路由（Phase 7 T2；Sup §6:132-134）。

门序（D6 钉死）：CSRF → 认证（get_v2_auth）→ get_v2_admin_auth 三重门 →
reason 门 → 幂等 begin（app 裸会话，admin 门之后——未过 12h 门者不能靠旧 key
重放绕过）→ admin 事务（服务 + admin_idem_store 同事务收口）→ 提交。GET 列表为
元数据读（免 reason 免幂等，Sup:128）；三个端点全挂 admin 三重门（读写全受门）。

token 红线（Sup:132）：明文 token 只进 outbox payload——响应/审计零 token
（服务层保证，本壳响应体仅邀请元数据）。路由一律相对路径（挂载前缀 /api/admin
由 main.py 单点提供）。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.api.v2.admin import (
    admin_idem_begin,
    admin_idem_store,
    get_v2_admin_auth,
    require_admin_reason,
)
from backend.v2 import admin_invitation_service, idempotency
from backend.v2.idempotency import require_key_header
from backend.v2.runtime import V2Runtime, get_v2_runtime
from backend.v2.session_service import V2AuthContext

router = APIRouter()

# 列表分页默认（服务层校验 1≤size≤100；Sup §7 列表信封附 total/page/size）
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20


class InvitationCreatePayload(BaseModel):
    """POST /invitations 载荷（reason 走 require_admin_reason：缺/空 400
    ADMIN_REASON_REQUIRED，不由 pydantic 必填——契约信封优先）。"""

    email: str
    expires_in_days: int
    reason: str | None = None


class InvitationRevokePayload(BaseModel):
    reason: str | None = None


@router.post("/invitations")
async def create_invitation(
    payload: InvitationCreatePayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """创建邀请（201；token 明文只走邮件 outbox，响应仅邀请元数据）。"""
    admin_id = str(ctx.user.id)
    body = payload.model_dump(exclude_unset=True)
    reason = require_admin_reason(body.get("reason"))
    body["reason"] = reason  # 原样参与 request_hash（D11：归一化不在壳层做）
    req_hash = idempotency.request_hash(body)
    replay = await admin_idem_begin(
        runtime, request=request, admin_id=admin_id, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with runtime.admin_factory() as db:
        async with db.begin():
            result = await admin_invitation_service.create_invitation(
                db,
                admin_id=admin_id,
                email=payload.email,
                expires_in_days=payload.expires_in_days,
                reason=reason,
                request_id=None,
            )
            await admin_idem_store(
                db,
                request=request,
                admin_id=admin_id,
                key=idem_key,
                req_hash=req_hash,
                status_code=201,
                payload={"data": result},
            )
    return JSONResponse(status_code=201, content={"data": result})


@router.get("/invitations")
async def list_invitations(
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    status: str | None = None,
    page: int = _DEFAULT_PAGE,
    size: int = _DEFAULT_PAGE_SIZE,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """邀请列表（状态过滤 open/consumed/expired/revoked + 分页；元数据读免 reason）。"""
    async with runtime.admin_factory() as db:
        result = await admin_invitation_service.list_invitations(
            db, status=status, page=page, size=size
        )
    return JSONResponse(status_code=200, content={"data": result})


@router.post("/invitations/{invitation_id}/revoke")
async def revoke_invitation(
    invitation_id: str,
    payload: InvitationRevokePayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """撤销未消费邀请（200；consumed → 409，过期未消费可撤销——服务层申报）。"""
    admin_id = str(ctx.user.id)
    body = payload.model_dump(exclude_unset=True)
    reason = require_admin_reason(body.get("reason"))
    body["reason"] = reason
    req_hash = idempotency.request_hash(body)
    replay = await admin_idem_begin(
        runtime, request=request, admin_id=admin_id, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with runtime.admin_factory() as db:
        async with db.begin():
            result = await admin_invitation_service.revoke_invitation(
                db,
                admin_id=admin_id,
                invitation_id=invitation_id,
                reason=reason,
                request_id=None,
            )
            await admin_idem_store(
                db,
                request=request,
                admin_id=admin_id,
                key=idem_key,
                req_hash=req_hash,
                status_code=200,
                payload={"data": result},
            )
    return JSONResponse(status_code=200, content={"data": result})
