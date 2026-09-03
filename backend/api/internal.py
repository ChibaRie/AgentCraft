"""内部接口：Pi 任务沙箱回调（Engineering Spec §6.8）。

仅供任务容器内扩展调用，不对外暴露。/internal/mcp/call 校验链：
X-Task-Token（任务级+实例级）→ body task_id 一致 → 快照能力上限 →
kill switch（Server/工具/绑定/敏感授权）→ 连接 Server 执行 tools/call。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.dependencies import get_pi_engine_manager
from backend.engine.pi_engine_manager import PiEngineManager
from backend.models.task import Task
from backend.services import mcp_service
from backend.services.task_token import TaskTokenInvalid, decode_task_token

router = APIRouter(tags=["internal"])

logger = logging.getLogger("agentcraft")


class TaskTokenUnauthorized(HTTPException):
    def __init__(self) -> None:
        # 统一信封（§6.1）；不给沙箱泄露具体校验层
        super().__init__(status.HTTP_401_UNAUTHORIZED, "任务令牌无效")


class MCPCallRequest(BaseModel):
    task_id: int
    server_id: int
    tool_name: str
    args: dict = {}


def _require_task_token(
    request: Request, payload: MCPCallRequest, manager: PiEngineManager
) -> dict:
    """X-Task-Token 三重校验：签名有效、任务一致、实例一致（旧容器令牌失效）。"""
    token = request.headers.get("X-Task-Token", "")
    try:
        claims = decode_task_token(token)
    except TaskTokenInvalid as exc:
        raise TaskTokenUnauthorized() from exc
    if claims["task_id"] != payload.task_id:
        raise TaskTokenUnauthorized()
    if manager.get_task_token(payload.task_id) != token:
        raise TaskTokenUnauthorized()
    return claims


@router.post("/mcp/call")
async def call_mcp(
    payload: MCPCallRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    manager: PiEngineManager = Depends(get_pi_engine_manager),
) -> dict[str, object]:
    _require_task_token(request, payload, manager)
    task = await db.get(Task, payload.task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    # 快照能力上限 + kill switch（任一失败即 403/404，§6.8）
    await mcp_service.validate_task_tool_call(db, task, payload.server_id, payload.tool_name)
    result = await mcp_service.execute_task_tool_call(
        db,
        settings,
        server_id=payload.server_id,
        tool_name=payload.tool_name,
        args=payload.args,
    )
    return {"data": result}


@router.post("/ui/response")
async def respond_ui(payload: dict[str, object]) -> dict[str, object]:
    # P1 预留：v1 由 PiEngine 内部自动应答 extension_ui_request，不暴露此接口
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/harness/check-code-style")
async def check_code_style(payload: dict[str, object]) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
