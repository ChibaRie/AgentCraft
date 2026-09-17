"""V2 用户 MCP 面路由（Phase 10 M2；契约草案见
docs/superpowers/plans/2026-09-17-v2-phase-10-mcp-face.md，Sup §4.6 重写随 M9 回写）。

端点（挂 /api 前缀，契约路径惯例同 Sup §10.11）：
- GET  /mcp/servers（认证只读，零 command/env/url 出参）；
- POST /mcp/servers（写，幂等 + CSRF；stdio 命令材料信封加密）；
- PUT/DELETE /mcp/servers/{id}（写，幂等 + CSRF）；
- POST /mcp/servers/{id}/discover（认证 + CSRF + 限流 mcp_discover 10/h，
  不幂等——沿 provider /test D10 形态）。stdio = subprocess 直拉 + stdio 握手
  （Phase 10 M3；mcp-sandbox 镜像形态属 M5）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.api.v2.schemas import McpServerCreateRequest, McpServerUpdateRequest
from backend.v2 import idempotency, mcp_service
from backend.v2.idempotency import require_key_header
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, get_v2_runtime, owner_session
from backend.v2.session_service import V2AuthContext, get_v2_auth

router = APIRouter()


@router.get("/mcp/servers")
async def list_mcp_servers(
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """当前用户 MCP server 列表（owner-RLS；出参零密文零 command/env/url）。"""
    async with owner_session(runtime, str(user_ctx.user.id)) as db:
        items = await mcp_service.list_servers(db)
    return JSONResponse(status_code=200, content={"data": items})


@router.post("/mcp/servers")
async def create_mcp_server(
    payload: McpServerCreateRequest,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """注册 MCP server（写端点：Idempotency-Key 必带；命中重放不携带 Set-Cookie）。"""
    updates = payload.model_dump(exclude_unset=True)
    outcome = await mcp_service.create_server(
        runtime,
        user_id=str(user_ctx.user.id),
        updates=updates,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(updates),
    )
    if isinstance(outcome, mcp_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content={"data": outcome})


@router.put("/mcp/servers/{server_id}")
async def update_mcp_server(
    server_id: str,
    payload: McpServerUpdateRequest,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """更新 MCP server（name/enabled 三态；Idempotency-Key 必带）。"""
    updates = payload.model_dump(exclude_unset=True)
    outcome = await mcp_service.update_server(
        runtime,
        user_id=str(user_ctx.user.id),
        server_id=server_id,
        updates=updates,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(updates),
    )
    if isinstance(outcome, mcp_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content={"data": outcome})


@router.delete("/mcp/servers/{server_id}")
async def delete_mcp_server(
    server_id: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """删除 MCP server（物理删 + 级联发现缓存 + 审计；Idempotency-Key 必带，无请求体）。"""
    outcome = await mcp_service.delete_server(
        runtime,
        user_id=str(user_ctx.user.id),
        server_id=server_id,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(None),
    )
    if isinstance(outcome, mcp_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content={"data": outcome})


@router.post("/mcp/servers/{server_id}/discover")
async def discover_mcp_server(
    server_id: str,
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """工具发现（认证 + CSRF + 限流 mcp_discover 10/h/用户；不幂等——D10 形态）。

    stdio → subprocess 直拉 + MCP stdio 握手（Phase 10 M3）；http → streamable
    HTTP 握手。任一传输失败 → 502 MCP_DISCOVER_FAILED（零上游细节泄漏）。
    """
    async with runtime.app_factory() as db:
        await enforce(
            db, scope="mcp_discover", subjects=[hmac_subject("user", str(user_ctx.user.id))]
        )
    result = await mcp_service.discover_server(
        runtime, user_id=str(user_ctx.user.id), server_id=server_id
    )
    return JSONResponse(status_code=200, content={"data": result})
