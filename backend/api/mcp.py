from fastapi import APIRouter, Depends, HTTPException, status

from backend.middleware.auth import get_current_user_id
from backend.schemas.mcp import MCPServerCreateRequest, MCPServerUpdateRequest, MCPToolUpdateRequest

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.post("/servers", status_code=status.HTTP_201_CREATED)
async def create_server(
    payload: MCPServerCreateRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.get("/servers")
async def list_servers(user_id: int = Depends(get_current_user_id)) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.get("/servers/{server_id}")
async def get_server(
    server_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.put("/servers/{server_id}")
async def update_server(
    server_id: int, payload: MCPServerUpdateRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/servers/{server_id}/discover")
async def discover_tools(
    server_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.put("/servers/{server_id}/tools/{tool_id}")
async def update_tool(
    server_id: int,
    tool_id: int,
    payload: MCPToolUpdateRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/servers/{server_id}/publish")
async def publish_server(
    server_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/servers/{server_id}/offline")
async def offline_server(
    server_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.delete("/servers/{server_id}")
async def delete_server(
    server_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
