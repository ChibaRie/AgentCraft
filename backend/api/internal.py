"""平台工具回调面（Phase 5 收窄后 + Phase 6 T7 全回调四端点）：

- /internal/harness/check-code-style：X-Task-Token 三重校验 +
  tool_catalog.enabled 第二校验（V1 面，零改动）；
- /internal/ui/response：501 占位（V1 面，零改动）；
- /internal/tools/*：V2 任务域四平台工具回调（Phase 6 T7，D2 全回调）——
  V2 专用校验链（D17）：decode_v2_task_token → claims.task_id=请求 task_id →
  round_id/instance=executor.tokens 登记表核验 → claims.lease_epoch ==
  task_rounds 当前值（fence 后旧令牌一律 401，Eng §3.3:85）→ D16 owner
  会话（owner 从 claims 取，免 admin 反查）→ assert_tool_enabled 第二校验
  （403 TOOL_REVOKED）→ permissions 服务端强制（TOOL_CALL_REJECTED 400）。
  permissions 形态闸（含请求形态/content_base64 解码）在 owner 会话内 fence
  之后执行——fenced 旧令牌即便携带畸形请求体也一律 401。
  用户 MCP 语义已下线（/internal/mcp/call 删除）。
"""

import base64
import logging
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.dependencies import get_pi_engine_manager
from backend.engine.pi_engine_manager import PiEngineManager
from backend.engine.platform_tools import PLATFORM_TOOLS
from backend.errors import AgentCraftError, ErrorCode
from backend.models.task import Task
from backend.services import harness_service
from backend.services.task_token import TaskTokenInvalid, decode_task_token
from backend.v2.models import TaskRound
from backend.v2.runtime import V2Runtime, get_optional_v2_runtime, get_v2_runtime, owner_session
from backend.v2.task_artifacts import register_output
from backend.v2.task_file_service import list_input_meta, read_input_bytes
from backend.v2.task_token import TaskTokenInvalid as V2TaskTokenInvalid
from backend.v2.task_token import decode_v2_task_token
from backend.v2.task_views import _strip_lease_fields, get_task_view
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


# ---------------------------------------------------------------------------
# V2 任务域平台工具回调（Phase 6 T7，D2 全回调 / D16 / D17）
# ---------------------------------------------------------------------------


class V2ToolRequestBase(BaseModel):
    task_id: str  # 模板恒带 task_id（extension 生成体 {task_id, ...args}）


class V2ReadTaskFileRequest(V2ToolRequestBase):
    file_name: str


class V2WriteOutputFileRequest(V2ToolRequestBase):
    file_name: str
    content_base64: str


class V2ListTaskFilesRequest(V2ToolRequestBase):
    pass


class V2QueryTaskStateRequest(V2ToolRequestBase):
    pass


def _tool_call_rejected(message: str) -> AgentCraftError:
    return AgentCraftError(ErrorCode.TOOL_CALL_REJECTED, message, http_status=400)


def _require_v2_task_token(request: Request, payload: V2ToolRequestBase, rt: V2Runtime) -> dict:
    """V2 任务令牌校验链前半（D17）：decode → claims.task_id=请求 task_id →
    round_id/instance=executor.tokens 登记表核验。

    全链失败统一 401（不给沙箱泄露具体校验层，V1 _require_task_token 同纪律）；
    观测记 task_id 与校验层，绝不记录令牌值（红线 §4.7）。claims.lease_epoch
    对 task_rounds 当前值的 fence 校验在 owner 会话内进行（D16）。
    """
    token = request.headers.get("X-Task-Token", "")
    try:
        claims = decode_v2_task_token(token)
    except V2TaskTokenInvalid as exc:
        logger.warning("V2 工具回调令牌校验失败 layer=signature task_id=%s", payload.task_id)
        raise TaskTokenUnauthorized() from exc
    if claims["task_id"] != str(payload.task_id):
        logger.warning("V2 工具回调令牌校验失败 layer=task-mismatch task_id=%s", payload.task_id)
        raise TaskTokenUnauthorized()
    executor = rt.executor
    registered = executor.tokens.get(claims["round_id"]) if executor is not None else None
    if registered is None or registered != token:
        logger.warning(
            "V2 工具回调令牌校验失败 layer=instance-mismatch task_id=%s", payload.task_id
        )
        raise TaskTokenUnauthorized()
    return claims


async def _assert_round_live(db: AsyncSession, claims: dict) -> None:
    """epoch fence（D17/Eng §3.3:85）：claims.lease_epoch == task_rounds 当前值
    且轮仍 running——fence 后旧容器令牌一律拒绝（401，与 V1 旧实例令牌同语义）。"""
    row = (
        await db.execute(
            select(TaskRound.state, TaskRound.lease_epoch).where(
                TaskRound.id == _uuid.UUID(claims["round_id"])
            )
        )
    ).first()
    if row is None or row.state != "running" or int(row.lease_epoch) != int(claims["lease_epoch"]):
        logger.warning(
            "V2 工具回调令牌校验失败 layer=epoch-fence task_id=%s round_id=%s",
            claims["task_id"],
            claims["round_id"],
        )
        raise TaskTokenUnauthorized()


def _assert_tool_permissions(tool_id: str, *, needs_paths: tuple[str, ...] = ()) -> None:
    """permissions 服务端强制（D2）：描述符声明缺失/形态不符/未声明所需路径 →
    400 TOOL_CALL_REJECTED。network:false 是容器面语义（回调端点本身不外呼），
    声明为真即描述符被篡改，拒绝。校验链位置（冻结全序）：epoch fence 与
    assert_tool_enabled 之后——只对活令牌分流 400，fenced 旧令牌在更早层 401。"""
    tool = PLATFORM_TOOLS.get((tool_id, "1"))
    perms = tool.permissions if tool is not None else None
    if not isinstance(perms, dict) or perms.get("network") is not False:
        raise _tool_call_rejected("工具权限声明缺失或形态非法")
    declared = perms.get("paths") or []
    for needed in needs_paths:
        if needed not in declared:
            raise _tool_call_rejected(f"工具权限未声明路径 {needed}")


def _reject_traversal(file_name: str) -> None:
    """permissions 路径形态强制：穿越形/分段名/点段名 → 400 TOOL_CALL_REJECTED
    （服务层防穿越为行键查询，本闸在 fence 之后的请求形态层拒绝）。"""
    if (
        not isinstance(file_name, str)
        or not file_name
        or "/" in file_name
        or "\\" in file_name
        or file_name in (".", "..")
    ):
        raise _tool_call_rejected("文件名路径形态非法")


@router.post("/tools/read-task-file")
async def read_task_file_tool(
    payload: V2ReadTaskFileRequest,
    request: Request,
    rt: V2Runtime = Depends(get_v2_runtime),
) -> dict[str, object]:
    """读取任务输入文件字节（read_task_file@1 回调；只认 inputs 根——
    read_input_bytes 行键防穿越）。

    校验链冻结全序：fence（401）先于 permissions 形态闸——fenced 旧令牌即便
    携带畸形请求体也一律 401，permissions 闸只对活令牌分流 400。"""
    claims = _require_v2_task_token(request, payload, rt)
    async with owner_session(rt, claims["owner_id"]) as db:
        await _assert_round_live(db, claims)
        await assert_tool_enabled(db, "read_task_file", "1")
        _assert_tool_permissions("read_task_file", needs_paths=("/task-files",))
        _reject_traversal(payload.file_name)
        content = await read_input_bytes(
            rt.storage,
            db,
            owner_id=claims["owner_id"],
            task_id=claims["task_id"],
            file_name=payload.file_name,
        )
    return {
        "data": {
            "file_name": payload.file_name,
            "content_base64": base64.b64encode(content).decode("ascii"),
            "size_bytes": len(content),
        }
    }


@router.post("/tools/write-output-file")
async def write_output_file_tool(
    payload: V2WriteOutputFileRequest,
    request: Request,
    rt: V2Runtime = Depends(get_v2_runtime),
) -> dict[str, object]:
    """登记容器产物（write_output_file@1 回调；写 outputs/artifacts 根——
    register_output TaskStorage 派生路径；round_id/lease_epoch 取自 claims）。

    校验链冻结全序：fence（401）先于 permissions 形态闸/content_base64 解码
    （同 read-task-file）——fenced 旧令牌即便携带畸形请求体也一律 401。"""
    claims = _require_v2_task_token(request, payload, rt)
    async with owner_session(rt, claims["owner_id"]) as db:
        await _assert_round_live(db, claims)
        await assert_tool_enabled(db, "write_output_file", "1")
        _assert_tool_permissions("write_output_file", needs_paths=("/outputs",))
        _reject_traversal(payload.file_name)
        try:
            content = base64.b64decode(payload.content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise _tool_call_rejected("content_base64 非法") from exc
        out = await register_output(
            db,
            rt.storage,
            owner_id=claims["owner_id"],
            task_id=claims["task_id"],
            file_name=payload.file_name,
            content=content,
            round_id=claims["round_id"],
            lease_epoch=claims["lease_epoch"],
        )
    return {"data": out}


@router.post("/tools/list-task-files")
async def list_task_files_tool(
    payload: V2ListTaskFilesRequest,
    request: Request,
    rt: V2Runtime = Depends(get_v2_runtime),
) -> dict[str, object]:
    """任务输入文件元数据（list_task_files@1 回调；list_input_meta 存活口径）。

    校验链冻结全序同 read/write：fence（401）先于 permissions 形态闸。"""
    claims = _require_v2_task_token(request, payload, rt)
    async with owner_session(rt, claims["owner_id"]) as db:
        await _assert_round_live(db, claims)
        await assert_tool_enabled(db, "list_task_files", "1")
        _assert_tool_permissions("list_task_files", needs_paths=("/task-files", "/outputs"))
        files = await list_input_meta(db, task_id=claims["task_id"])
    return {"data": {"files": files}}


@router.post("/tools/query-task-state")
async def query_task_state_tool(
    payload: V2QueryTaskStateRequest,
    request: Request,
    rt: V2Runtime = Depends(get_v2_runtime),
) -> dict[str, object]:
    """任务状态摘要（query_task_state@1 回调）：D14 视图经服务端构造器剥除
    lease_* 字段（剥除式而非 400 拒绝式；描述符 exclude 声明一并强制）。

    校验链冻结全序同 read/write：fence（401）先于 permissions 形态闸。"""
    claims = _require_v2_task_token(request, payload, rt)
    async with owner_session(rt, claims["owner_id"]) as db:
        await _assert_round_live(db, claims)
        await assert_tool_enabled(db, "query_task_state", "1")
        _assert_tool_permissions("query_task_state")
        # 剥除谓词 = lease_ 前缀 ∪ 描述符 permissions.exclude 声明（D2：
        # permissions 服务端强制；描述符被扩列时响应面同步收口）
        perms = PLATFORM_TOOLS[("query_task_state", "1")].permissions or {}
        excluded = frozenset(str(k) for k in (perms.get("exclude") or ()))
        view = await get_task_view(db, owner_id=claims["owner_id"], task_id=claims["task_id"])
    return {"data": {"task": _strip_lease_fields(view, excluded)}}
