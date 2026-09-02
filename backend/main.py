import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api import api_router, internal_router
from backend.services.user_service import UserSystemError

logger = logging.getLogger("agentcraft")

app = FastAPI(title="AgentCraft API", version="0.4.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api")
app.include_router(internal_router, prefix="/internal")


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
