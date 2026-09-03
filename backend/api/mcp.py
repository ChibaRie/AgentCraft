"""MCP 管理接口（Engineering Spec §6.7）。

Server 全生命周期（注册/发现/工具开关/发布/下架/删除）+ env 加密信封
（API 只回变量名）。错误语义：归属统一 404；状态/参数 400；引用未清 409；
上游连接失败 502。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.middleware.auth import get_current_user_id
from backend.schemas.mcp import (
    MCPServerCreateRequest,
    MCPServerUpdateRequest,
    MCPToolUpdateRequest,
)
from backend.services import mcp_service

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.post("/servers", status_code=201)
async def create_server(
    payload: MCPServerCreateRequest,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    server = await mcp_service.create_server(
        db,
        user_id,
        name=payload.name,
        description=payload.description,
        transport=payload.transport,
        command=payload.command,
        url=payload.url,
        env_vars=payload.env_vars,
        settings=settings,
    )
    return {"data": mcp_service.server_payload(server, settings=settings)}


@router.get("/servers")
async def list_servers(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    servers, total = await mcp_service.list_servers(db, user_id, page, size)
    return {
        "data": [mcp_service.server_payload(server, settings=settings) for server in servers],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/servers/{server_id}")
async def get_server(
    server_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    server, tools = await mcp_service.get_server_detail(db, user_id, server_id)
    return {
        "data": mcp_service.server_payload(
            server,
            settings=settings,
            with_tools=[mcp_service.tool_payload(tool) for tool in tools],
        )
    }


@router.put("/servers/{server_id}")
async def update_server(
    server_id: int,
    payload: MCPServerUpdateRequest,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    server = await mcp_service.update_server(
        db,
        user_id,
        server_id,
        name=payload.name,
        description=payload.description,
        command=payload.command,
        url=payload.url,
        env_vars=payload.env_vars,
        env_vars_provided="env_vars" in payload.model_fields_set,
        settings=settings,
    )
    return {"data": mcp_service.server_payload(server, settings=settings)}


@router.post("/servers/{server_id}/discover")
async def discover_tools(
    server_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    tools = await mcp_service.discover_tools(db, user_id, server_id, settings)
    return {"data": {"tools": tools}}


@router.put("/servers/{server_id}/tools/{tool_id}")
async def update_tool(
    server_id: int,
    tool_id: int,
    payload: MCPToolUpdateRequest,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    tool = await mcp_service.update_tool(
        db,
        user_id,
        server_id,
        tool_id,
        enabled=payload.enabled,
    )
    return {"data": mcp_service.tool_payload(tool)}


@router.post("/servers/{server_id}/publish")
async def publish_server(
    server_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    server = await mcp_service.publish_server(db, user_id, server_id)
    return {"data": {"id": server.id, "status": server.status}}


@router.post("/servers/{server_id}/offline")
async def offline_server(
    server_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    server = await mcp_service.offline_server(db, user_id, server_id)
    return {"data": {"id": server.id, "status": server.status}}


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    await mcp_service.delete_server(db, user_id, server_id)
    return {"data": {"message": "deleted"}}
