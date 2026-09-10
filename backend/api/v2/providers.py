"""V2 Provider 面路由（契约：Supplement §3，A1 前缀 /api/v2——裁决 D1）。

端点分类（Phase 3 交付节奏）：
- Task 6：GET /providers/catalog（认证只读）、GET /providers（认证只读，active 行）；
- Task 7：POST /providers（写，幂等 + CSRF）；
- Task 9：PUT/DELETE /providers/{id}（写，幂等 + CSRF + 联动）；
- Task 10：POST /providers/{id}/test（认证 + CSRF + 限流，不幂等——D10）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.v2 import provider_service
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
