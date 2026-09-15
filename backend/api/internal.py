"""平台工具回调面（Phase 5 收窄后 + Phase 6 T7 全回调四端点 + Phase 8 T5b grant）：

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
  用户 MCP 语义已下线（/internal/mcp/call 删除）；
- /internal/provider-grant：provider-proxy 专用 Key 兑换（Phase 8 T5b，
  Sup §10.10 D16① 六钉）——proxy 专用凭据（env PROXY_GRANT_SECRET）+
  V2 任务令牌（仅定位 round）+ epoch fence 同构断言，响应仅从已验 claims
  派生，审计先行（fail-closed），响应 no-store。
"""

import base64
import hmac
import logging
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.dependencies import get_pi_engine_manager
from backend.engine.pi_engine_manager import PiEngineManager
from backend.engine.platform_tools import PLATFORM_TOOLS
from backend.errors import AgentCraftError, ErrorCode
from backend.models.task import Task
from backend.services import harness_service
from backend.services.task_token import TaskTokenInvalid, decode_task_token
from backend.v2.models import AuditLog, ProviderCatalog, TaskRound, UserProvider
from backend.v2.models import Task as V2Task  # V2 任务行（owner_id 语义；V1 Task 属 sqlite 面）
from backend.v2.provider_crypto import key_sealer
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


# ---------------------------------------------------------------------------
# provider-grant（Phase 8 T5b，Sup §10.10 D16① 六钉）——provider-proxy 专用
# ---------------------------------------------------------------------------

_GRANT_AUDIT_ACTION = "provider.grant.issue"


def _require_proxy_grant_credential(request: Request, settings: Settings) -> None:
    """钉一：proxy 专用凭据——X-Proxy-Grant-Secret 必须等于 env PROXY_GRANT_SECRET。

    独立 secret 仅注入 provider-proxy env（task 容器 env 禁出现，测试以常量
    清单钉）；compare_digest 恒定时间比较；服务端未配置/请求缺失/不符统一
    401，与令牌失败同信封（不区分校验层，internal 纪律）。观测只记层名，
    不记任何凭据值（红线 §4.7）。
    """
    expected = settings.PROXY_GRANT_SECRET
    provided = request.headers.get("X-Proxy-Grant-Secret", "")
    if (
        not expected
        or not provided
        or not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    ):
        logger.warning("provider-grant 校验失败 layer=proxy-credential")
        raise TaskTokenUnauthorized()


@router.post("/provider-grant")
async def provider_grant(
    request: Request,
    settings: Settings = Depends(get_settings),
    rt: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """provider-proxy 专用 Key 兑换（Sup §10.10；内部端点，不入公开 API 面）。

    校验链（全部失败统一 401 同信封，不分层）：proxy 凭据（钉一）→
    decode_v2_task_token（X-Task-Token 仅定位 round，非授权因子）→ epoch
    fence 复用 /internal/tools 同构断言（钉三：lease_epoch==当前值且轮
    running，settle 后同令牌自然 401）→ 任务/Provider 行按 fence 后 claims
    经 owner 会话（RLS 圈定）定位。响应 {model, provider:{api_key,
    base_target}} 仅从已验 claims 派生（钉二，无 client 可覆写位）；Key
    解密 AAD 绑定 claims 定位的 provider 行（钉五）；发 Key 先经 admin 会话
    审计（钉六，audit_logs app role 零授权；audit 失败不发 Key——fail-closed）；
    响应 no-store（缓存钉）。明文 Key 仅存活于本协程内存，禁缓存禁日志。
    """
    _require_proxy_grant_credential(request, settings)
    try:
        claims = decode_v2_task_token(request.headers.get("X-Task-Token", ""))
    except V2TaskTokenInvalid as exc:
        logger.warning("provider-grant 校验失败 layer=signature")
        raise TaskTokenUnauthorized() from exc

    async with owner_session(rt, claims["owner_id"]) as db:
        await _assert_round_live(db, claims)  # 钉三 fence（/internal/tools 同构断言）
        task = (
            await db.execute(select(V2Task).where(V2Task.id == _uuid.UUID(claims["task_id"])))
        ).scalar_one_or_none()
        if task is None:
            logger.warning(
                "provider-grant 校验失败 layer=task-missing task_id=%s", claims["task_id"]
            )
            raise TaskTokenUnauthorized()
        provider = (
            await db.execute(select(UserProvider).where(UserProvider.id == task.provider_id))
        ).scalar_one_or_none()
        catalog = (
            await db.execute(
                select(ProviderCatalog).where(ProviderCatalog.id == task.provider_catalog_id)
            )
        ).scalar_one_or_none()
        if provider is None or provider.status != "active" or catalog is None:
            logger.warning(
                "provider-grant 校验失败 layer=provider-unusable task_id=%s", claims["task_id"]
            )
            raise TaskTokenUnauthorized()
        # 钉二：响应字段全部取自行数据，行定位全部来自已验 claims（RLS 圈定）
        model = task.provider_model_id
        provider_id = str(provider.id)
        key_ciphertext = provider.key_ciphertext
        dek_wrapped = provider.dek_wrapped
        base_target = f"https://{catalog.allowed_host}{catalog.path_prefix}"

    # 钉六：发 Key 先审计（admin 会话独立事务——audit_logs 无 RLS 且 app role
    # 零授权）；审计失败异常向上 → 500，Key 不出控制面（fail-closed）
    async with rt.admin_factory() as admin_db:
        async with admin_db.begin():
            admin_db.add(
                AuditLog(
                    action=_GRANT_AUDIT_ACTION,
                    target_type="task",
                    target_id=_uuid.UUID(claims["task_id"]),
                    reason="provider grant issued for running round",
                    actor_id=None,
                    detail={
                        "task_id": claims["task_id"],
                        "round_id": claims["round_id"],
                        "provider_id": provider_id,
                    },
                )
            )

    # 钉五：解密 AAD 绑定 claims 定位的 provider 行（key_sealer 双层解封）
    api_key = key_sealer().open(key_ciphertext, dek_wrapped, provider_id=provider_id)
    return JSONResponse(
        {"model": model, "provider": {"api_key": api_key, "base_target": base_target}},
        headers={"Cache-Control": "no-store"},
    )
