import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api import internal_router
from backend.api.v2 import v2_api_router
from backend.api.v2.admin import admin_api_router
from backend.config import get_settings
from backend.dependencies import get_pi_engine_manager
from backend.errors import AgentCraftError
from backend.logging_config import configure_logging
from backend.middleware.upload_guard import UploadSizeGuardMiddleware
from backend.services.harness_service import HarnessServiceError
from backend.v2.deletion_service import deletion_sweep_loop
from backend.v2.mailer import transport_from_settings
from backend.v2.outbox import outbox_loop
from backend.v2.runtime import v2_runtime_from_settings
from backend.v2.task_dispatcher import _instance_id, dispatcher_loop
from backend.v2.task_executor import RoundExecutor, executor_loop
from backend.v2.task_streams import TaskStreamRegistry

logger = logging.getLogger("agentcraft")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动巡检：密钥逃生舱告警、引擎后台巡检循环、V2 后台作业编排。"""
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    if settings.ALLOW_INSECURE_SECRETS:
        # 逃生舱不可静默：置 true 时必须留下可见告警（生产禁止）
        logger.warning("ALLOW_INSECURE_SECRETS=true：密钥校验已跳过，仅限开发/测试环境")
    # §7.2 空闲回收 / §7.8.1 看门狗：后台巡检循环
    get_pi_engine_manager().ensure_background()
    # V2 outbox 派发循环：仅双 DSN 齐备的 V2 模式启动（V1-only 行为完全不变）。
    # transport 选择在启动时解析——未知 MAIL_TRANSPORT 值启动即失败（fail fast）。
    v2_rt = v2_runtime_from_settings()
    outbox_task: asyncio.Task | None = None
    sweep_task: asyncio.Task | None = None
    dispatcher_task: asyncio.Task | None = None
    executor_task: asyncio.Task | None = None
    if v2_rt is not None:
        transport = transport_from_settings(settings)
        outbox_task = asyncio.create_task(outbox_loop(v2_rt, transport), name="outbox-dispatcher")
        # 注销宽限期到期清理作业（60s 轮询；outbox_loop 同款 try/except-continue 形态）
        sweep_task = asyncio.create_task(deletion_sweep_loop(v2_rt), name="deletion-sweeper")
        # 任务域执行器生产接线（Phase 6 T6b，D3 单实例）：streams 注册表 + 执行器
        # 挂 runtime（dispatch 领轮经 executor.notify 唤醒；D18 注销钩子经
        # runtime.executor 放弃通知）→ executor_loop 常驻（notify 消费 + 周期对账）
        executor = RoundExecutor(v2_rt, streams=TaskStreamRegistry(), instance_id=_instance_id())
        v2_rt.executor = executor
        executor_task = asyncio.create_task(executor_loop(v2_rt), name="task-executor")
        # 任务域调度循环（Phase 6 T5）：领槽/回收/双清扫四作业串行（D16 两段式；
        # V2_TASK.dispatcher_poll_seconds 轮询；executor 缺位时 dispatch 空转免领）
        dispatcher_task = asyncio.create_task(dispatcher_loop(v2_rt), name="task-dispatcher")
        logger.info("V2 outbox dispatcher started (transport=%s)", settings.MAIL_TRANSPORT)
    try:
        yield
    finally:
        # 先停后台任务，再释放引擎；CancelledError 穿透各循环的常规异常捕获
        # （executor 最后取消：先停 dispatcher 防新领取，执行链在取消展开中经
        # _run_round finally 释放引擎/容器/续约协程——T6b 关停验证钉无挂起）
        for background in (outbox_task, sweep_task, dispatcher_task, executor_task):
            if background is not None:
                background.cancel()
                with suppress(asyncio.CancelledError):
                    await background
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
app.include_router(internal_router, prefix="/internal")
# V2 契约路径挂载（Phase 8 T13，Sup §10.1 路径切换总表）：/api/auth/*、
# /api/tasks/*、/api/health 等一律为契约路径（实现期暂挂 /api/v2 前缀已随
# cutover 摘除；V1 无门 /api/health 随 V1 面删除，由带 runtime 门的 V2 探针接位）。
# admin 面本就在契约路径 /api/admin，不变。
app.include_router(v2_api_router, prefix="/api")
app.include_router(admin_api_router, prefix="/api/admin")


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


@app.exception_handler(HarnessServiceError)
async def harness_service_error_handler(
    _request: Request, exc: HarnessServiceError
) -> JSONResponse:
    # /internal/harness 冻结面错误信封（D15-Option1 保留语义）：形状与原
    # UserSystemError 处理器一致 {error:{code,message}}，status_code/code
    # 由 harness_service 错误类自带（400 INVALID_PATH / 502 RUFF_EXECUTION_FAILED）
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
