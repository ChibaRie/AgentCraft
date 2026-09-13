"""平台工具回调面（Phase 5 收窄后）：/internal/harness/check-code-style
（X-Task-Token 三重校验 + tool_catalog.enabled 第二校验）与 /internal/ui/response
501 占位。用户 MCP 语义已下线（/internal/mcp/call 删除）。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.dependencies import get_pi_engine_manager
from backend.engine.pi_engine_manager import PiEngineManager
from backend.models.task import Task
from backend.services import harness_service
from backend.services.task_token import TaskTokenInvalid, decode_task_token
from backend.v2.runtime import V2Runtime, get_optional_v2_runtime
from backend.v2.tool_service import assert_tool_enabled

router = APIRouter(tags=["internal"])

logger = logging.getLogger("agentcraft")


class TaskTokenUnauthorized(HTTPException):
    def __init__(self) -> None:
        # 统一信封（§6.1）；不给沙箱泄露具体校验层
        super().__init__(status.HTTP_401_UNAUTHORIZED, "任务令牌无效")


class HarnessCheckRequest(BaseModel):
    task_id: int
    path: str | None = None


class UiResponseRequest(BaseModel):
    task_id: int


def _require_task_token(
    request: Request,
    payload: HarnessCheckRequest | UiResponseRequest,
    manager: PiEngineManager,
) -> dict:
    """X-Task-Token 三重校验：签名有效、任务一致、实例一致（旧容器令牌失效）。

    观测（红线 §4.7）：失败路径记 task_id 与校验层，绝不记录令牌值。
    """
    token = request.headers.get("X-Task-Token", "")
    try:
        claims = decode_task_token(token)
    except TaskTokenInvalid as exc:
        logger.warning("内部回调任务令牌校验失败 layer=signature task_id=%s", payload.task_id)
        raise TaskTokenUnauthorized() from exc
    if claims["task_id"] != payload.task_id:
        logger.warning("内部回调任务令牌校验失败 layer=task-mismatch task_id=%s", payload.task_id)
        raise TaskTokenUnauthorized()
    if manager.get_task_token(payload.task_id) != token:
        logger.warning(
            "内部回调任务令牌校验失败 layer=instance-mismatch task_id=%s", payload.task_id
        )
        raise TaskTokenUnauthorized()
    return claims


@router.post("/harness/check-code-style")
async def check_code_style(
    payload: HarnessCheckRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    manager: PiEngineManager = Depends(get_pi_engine_manager),
    rt: V2Runtime | None = Depends(get_optional_v2_runtime),
) -> dict[str, object]:
    """ruff format --check + ruff check（容器回调；30s 超时在服务层）。

    目录第二校验（Phase 5 T4）：V2 运行时在场时校验 check_code_style@1 在
    tool_catalog 中启用（kill switch），不存在/停用统一 403 TOOL_REVOKED。
    """
    _require_task_token(request, payload, manager)
    if rt is not None:  # V2 可选运行时（main.py:62）：未配置双 DSN 时跳过目录校验
        async with rt.app_factory() as v2_db:
            await assert_tool_enabled(v2_db, "check_code_style", "1")
    task = await db.get(Task, payload.task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    workdir_root = manager.resolve_workdir_host(task.workdir)
    target = await harness_service.resolve_workspace_path(workdir_root, payload.path)
    result = await harness_service.run_ruff_checks(target)
    return {"data": result}


@router.post("/ui/response")
async def respond_ui(
    payload: UiResponseRequest,
    request: Request,
    manager: PiEngineManager = Depends(get_pi_engine_manager),
) -> dict[str, object]:
    # P1 预留：v1 由 PiEngine 内部自动应答 extension_ui_request，不暴露此接口。
    # 即便 501 占位也先验任务令牌，/internal 不留未鉴权面
    _require_task_token(request, payload, manager)
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
