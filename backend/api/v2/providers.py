"""V2 Provider 面路由（契约：Supplement §3，A1 前缀 /api/v2——裁决 D1）。

端点分类（Phase 3 交付节奏）：
- Task 6：GET /providers/catalog（认证只读）、GET /providers（认证只读，active 行）；
- Task 7：POST /providers（写，幂等 + CSRF）；
- Task 9：PUT/DELETE /providers/{id}（写，幂等 + CSRF + 联动）；
- Task 10：POST /providers/{id}/test（认证 + CSRF + 限流，不幂等——D10）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.api.v2.schemas import ProviderCreateRequest, ProviderUpdateRequest
from backend.errors import AgentCraftError, ErrorCode
from backend.utils.crypto import EncryptionError
from backend.v2 import idempotency, provider_service
from backend.v2.idempotency import require_key_header
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, get_v2_runtime, owner_session
from backend.v2.session_service import V2AuthContext, get_v2_auth

router = APIRouter()


@router.get("/providers/catalog")
async def list_provider_catalog(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """启用中的 Provider 目录（登录用户可读，Sup §3；provider_catalog 无 RLS，服务层过滤）。"""
    async with runtime.app_factory() as db:
        items = await provider_service.list_catalog(db)
    return JSONResponse(status_code=200, content={"data": items})


@router.get("/providers")
async def list_providers(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """当前用户 active Provider 列表（owner-RLS + revoked 不可见，D13）。"""
    async with owner_session(runtime, str(user_ctx.user.id)) as db:
        items = await provider_service.list_user_providers(db)
    return JSONResponse(status_code=200, content={"data": items})


@router.post("/providers")
async def create_provider(
    payload: ProviderCreateRequest,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """创建 BYOK Provider（写端点：Idempotency-Key 必带，A5；命中重放不携带 Set-Cookie）。"""
    updates = payload.model_dump(exclude_unset=True)
    outcome = await provider_service.create_provider(
        runtime,
        user_id=str(user_ctx.user.id),
        updates=updates,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(updates),
    )
    if isinstance(outcome, provider_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content={"data": outcome})


@router.put("/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    payload: ProviderUpdateRequest,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """更新 BYOK Provider（写端点：Idempotency-Key 必带；api_key 两态，D2）。"""
    updates = payload.model_dump(exclude_unset=True)
    outcome = await provider_service.update_provider(
        runtime,
        user_id=str(user_ctx.user.id),
        provider_id=provider_id,
        updates=updates,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(updates),
    )
    if isinstance(outcome, provider_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content={"data": outcome})


@router.post("/providers/{provider_id}/test")
async def test_provider(
    provider_id: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """连通性测试（认证 + CSRF + 限流 provider_test 10/h；不幂等——D10）。

    EncryptionError → 400 KEY_VERSION_REVOKED（干净文案，不泄材料）。
    """
    async with runtime.app_factory() as db:
        await enforce(
            db, scope="provider_test", subjects=[hmac_subject("user", str(user_ctx.user.id))]
        )
    try:
        result = await provider_service.test_provider_connectivity(
            runtime, user_id=str(user_ctx.user.id), provider_id=provider_id
        )
    except EncryptionError:
        raise AgentCraftError(
            ErrorCode.KEY_VERSION_REVOKED, "Provider Key 不可用或已失效", http_status=400
        ) from None
    return JSONResponse(status_code=200, content={"data": result})


@router.delete("/providers/{provider_id}")
async def revoke_provider(
    provider_id: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """撤销 BYOK Provider（软撤 + 联动；Idempotency-Key 必带；无请求体）。"""
    outcome = await provider_service.revoke_provider(
        runtime,
        user_id=str(user_ctx.user.id),
        provider_id=provider_id,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(None),
    )
    if isinstance(outcome, provider_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content={"data": outcome})
