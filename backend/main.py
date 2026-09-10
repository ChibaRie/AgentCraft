import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api import api_router, internal_router
from backend.api.v2 import v2_api_router
from backend.config import get_settings
from backend.database import async_session_factory
from backend.dependencies import get_pi_engine_manager
from backend.errors import AgentCraftError
from backend.logging_config import configure_logging
from backend.middleware.upload_guard import UploadSizeGuardMiddleware
from backend.services.file_service import sweep_stale_storage
from backend.services.user_service import UserSystemError
from backend.services.workspace import CANONICAL_AGENT_ROOT
from backend.v2.mailer import transport_from_settings
from backend.v2.outbox import outbox_loop
from backend.v2.runtime import v2_runtime_from_settings

logger = logging.getLogger("agentcraft")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动巡检（§6.6/§10.2）：校验配置一致性、确保存储根存在、清理崩溃遗留。"""
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    if settings.ALLOW_INSECURE_SECRETS:
        # 逃生舱不可静默：置 true 时必须留下可见告警（生产禁止）
        logger.warning("ALLOW_INSECURE_SECRETS=true：密钥校验已跳过，仅限开发/测试环境")
    if settings.AGENTCRAFT_WORKSPACE_ROOT != CANONICAL_AGENT_ROOT:
        # tasks.workdir 的 CHECK 约束硬编码此前缀；可配置化需同步迁移约束
        raise RuntimeError(
            "AGENTCRAFT_WORKSPACE_ROOT 必须为 /workspaces/authorized"
            "（tasks.workdir CHECK 约束硬编码）"
        )
    Path(settings.HOST_WORKSPACE_ROOT).mkdir(parents=True, exist_ok=True)
    task_file_root = Path(settings.HOST_DATA_ROOT) / "task-files"
    task_file_root.mkdir(parents=True, exist_ok=True)
    try:
        stats = await sweep_stale_storage(async_session_factory, task_file_root)
        if stats["staging_batches"] or stats["orphan_files"]:
            logger.info("启动清理完成：%s", stats)
    except Exception:
        # 清理失败不阻塞启动，下次启动重试
        logger.exception("启动文件巡检失败")
    # §7.2 空闲回收 / §7.8.1 看门狗：后台巡检循环
    get_pi_engine_manager().ensure_background()
    # V2 outbox 派发循环：仅双 DSN 齐备的 V2 模式启动（V1-only 行为完全不变）。
    # transport 选择在启动时解析——未知 MAIL_TRANSPORT 值启动即失败（fail fast）。
    v2_rt = v2_runtime_from_settings()
    outbox_task: asyncio.Task | None = None
    if v2_rt is not None:
        transport = transport_from_settings(settings)
        outbox_task = asyncio.create_task(outbox_loop(v2_rt, transport), name="outbox-dispatcher")
        logger.info("V2 outbox dispatcher started (transport=%s)", settings.MAIL_TRANSPORT)
    try:
        yield
    finally:
        # 先停派发任务，再释放引擎；CancelledError 穿透 outbox_loop 的常规异常捕获
        if outbox_task is not None:
            outbox_task.cancel()
            with suppress(asyncio.CancelledError):
                await outbox_task
        # T4 review 承接：lifecycle 持有的 runtime 用异步释放（await engine.dispose()），
        # 不走 V2Runtime.close() 的同步 dispose 路径
        if v2_rt is not None:
            for engine in v2_rt.engines:
                await engine.dispose()


app = FastAPI(title="AgentCraft API", version="0.4.0", lifespan=lifespan)
# 单请求体量上限 = 单次文件数 × 单文件上限 + 32MB 表单余量（防 multipart 预落盘 DoS）
_settings = get_settings()
app.add_middleware(
    UploadSizeGuardMiddleware,
    max_bytes=_settings.UPLOAD_MAX_FILES_PER_REQUEST * _settings.UPLOAD_MAX_FILE_BYTES
    + 32 * 1024 * 1024,
)
app.add_middleware(
    CORSMiddleware,
    # 5173 可能落入 Windows WinNAT 保留段（5159-5258），5260 为备用前端端口
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5260",
        "http://localhost:5260",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api")
app.include_router(internal_router, prefix="/internal")
app.include_router(v2_api_router, prefix="/api/v2")


def _error_payload(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}}


# Engineering Spec §6.1：失败响应统一为 {error: {code, message}}
_STATUS_CODE_NAMES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    501: "NOT_IMPLEMENTED",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    507: "INSUFFICIENT_STORAGE",
}


@app.exception_handler(UserSystemError)
async def user_system_error_handler(_request: Request, exc: UserSystemError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(exc.code, str(exc)),
    )


@app.exception_handler(AgentCraftError)
async def agentcraft_error_handler(_request: Request, exc: AgentCraftError) -> JSONResponse:
    # V2 统一错误（Supplement §7）；headers 透传（如 401 Retry-After），None = 无附加头
    return JSONResponse(
        status_code=exc.http_status,
        content=_error_payload(exc.code.value, exc.message),
        headers=exc.headers,
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        code = str(detail["code"])
        message = str(detail.get("message", ""))
    else:
        code = _STATUS_CODE_NAMES.get(exc.status_code, "ERROR")
        message = detail if isinstance(detail, str) else "请求处理失败"
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_payload(code, message),
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    summary = "; ".join(
        f"{'.'.join(str(part) for part in error['loc'][1:]) or 'body'}: {error['msg']}"
        for error in exc.errors()
    )
    return JSONResponse(
        status_code=400,
        content=_error_payload("VALIDATION_ERROR", f"请求参数校验失败：{summary}"),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    # 兜底：未捕获异常也返回统一信封，服务端记录完整堆栈，不向客户端泄露内部信息
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content=_error_payload("INTERNAL_ERROR", "服务器内部错误，请稍后重试"),
    )


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {"data": {"status": "ok", "version": "0.4.0"}}
