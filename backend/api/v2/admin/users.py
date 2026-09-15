"""admin 用户管理路由（Phase 7 T3a；Sup §6:135-136/139-140）。

门序（D6 钉死，admin_invitation_service 同构）：CSRF → 认证（get_v2_auth）→
get_v2_admin_auth 三重门 → reason 门 → 幂等 begin（app 裸会话）→ admin 事务
（服务 + admin_idem_store 同事务收口）→ 提交。GET 列表/详情为元数据读（免
reason 免幂等）；读写全受 admin 三重门（Sup:126）。路由一律相对路径（挂载
前缀 /api/admin 由 main.py 单点提供）。

suspend/unsuspend 归 T3b（本模块后续任务追加，不在 T3a 范围）。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.api.v2.admin import (
    admin_idem_begin,
    admin_idem_store,
    get_v2_admin_auth,
    require_admin_reason,
)
from backend.v2 import admin_user_service, idempotency
from backend.v2.idempotency import require_key_header
from backend.v2.runtime import V2Runtime, get_v2_runtime
from backend.v2.session_service import V2AuthContext

router = APIRouter()

# 列表分页默认（服务层校验 1≤size≤100；Sup §7 列表信封附 total/page/size）
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20


class QuotasUpdatePayload(BaseModel):
    """PUT /users/{id}/quotas 载荷：四维字段可选；extra=forbid 防未知维度静默忽略，
    strict 拒绝 bool/float→int 宽松强转（true 到服务层已变 1 的语义漂移）。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    max_daily_tasks: int | None = None
    max_active_tasks: int | None = None
    max_running_tasks: int | None = None
    max_retained_storage_bytes: int | None = None
    reason: str | None = None


class EntitlementPayload(BaseModel):
    """POST/DELETE /users/{id}/entitlements 载荷（kind + reason；reason 走
    require_admin_reason——缺/空 400 ADMIN_REASON_REQUIRED，不由 pydantic 必填）。"""

    kind: str
    reason: str | None = None


@router.get("/users")
async def list_users(
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    email_prefix: str | None = None,
    status: str | None = None,
    page: int = _DEFAULT_PAGE,
    size: int = _DEFAULT_PAGE_SIZE,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """用户列表（email 前缀/状态过滤 + 分页；元数据读免 reason，Sup:135）。"""
    async with runtime.admin_factory() as db:
        result = await admin_user_service.list_users(
            db, email_prefix=email_prefix, status=status, page=page, size=size
        )
    return JSONResponse(status_code=200, content={"data": result})


@router.get("/users/{user_id}")
async def get_user_detail(
    user_id: str,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """用户详情（配额/用量/任务计数；不含任何 Key 材料——Sup:136 红线）。"""
    async with runtime.admin_factory() as db:
        result = await admin_user_service.get_user_detail(db, user_id=user_id)
    return JSONResponse(status_code=200, content={"data": result})


@router.put("/users/{user_id}/quotas")
async def update_user_quotas(
    user_id: str,
    payload: QuotasUpdatePayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """调整四维配额（upsert；审计 user.quotas.update before/after，Sup:139）。"""
    admin_id = str(ctx.user.id)
    body = payload.model_dump(exclude_unset=True)
    reason = require_admin_reason(body.get("reason"))
    body["reason"] = reason  # 原样参与 request_hash（D11：归一化不在壳层做）
    quotas = {k: v for k, v in body.items() if k != "reason"}
    req_hash = idempotency.request_hash(body)
    replay = await admin_idem_begin(
        runtime, request=request, admin_id=admin_id, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with runtime.admin_factory() as db:
        async with db.begin():
            result = await admin_user_service.update_quotas(
                db,
                admin_id=admin_id,
                user_id=user_id,
                quotas=quotas,
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


@router.post("/users/{user_id}/entitlements")
async def grant_entitlement(
    user_id: str,
    payload: EntitlementPayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """授予 entitlement（201；kind 词表校验；one_active 冲突 409；Sup:140）。"""
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
            result = await admin_user_service.grant_entitlement(
                db,
                admin_id=admin_id,
                user_id=user_id,
                kind=payload.kind,
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


@router.delete("/users/{user_id}/entitlements")
async def revoke_entitlement(
    user_id: str,
    payload: EntitlementPayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """撤销 entitlement（硬删活跃行；0 行统一 404；DELETE 幂等重放原响应）。"""
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
            result = await admin_user_service.revoke_entitlement(
                db,
                admin_id=admin_id,
                user_id=user_id,
                kind=payload.kind,
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
