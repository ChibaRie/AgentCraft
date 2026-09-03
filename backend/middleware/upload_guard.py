"""上传体量守卫：在进入 multipart 解析前拒绝超量请求。

Starlette 会在端点函数（含认证依赖）之前完整解析 multipart 并把文件 part
落到系统临时目录，应用层限额来不及生效；对 /api/tasks/{id}/files 检查
Content-Length 超限即 413，避免认证用户用单个请求占满临时磁盘。

只信任 Content-Length 头（httpx/浏览器 multipart 必带）；chunked 无长度
头的请求由应用层限额兜底。
"""

import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_UPLOAD_PATH_PATTERN = re.compile(r"^/api/tasks/\d+/files$")


class UploadSizeGuardMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST":
            path = scope.get("path", "")
            if _UPLOAD_PATH_PATTERN.match(path):
                content_length = Headers(scope=scope).get("content-length")
                if content_length and content_length.isdigit():
                    if int(content_length) > self.max_bytes:
                        response = JSONResponse(
                            status_code=413,
                            content={
                                "error": {
                                    "code": "FILE_REQUEST_TOO_LARGE",
                                    "message": f"请求体超过上传上限 {self.max_bytes} 字节",
                                }
                            },
                        )
                        await response(scope, receive, send)
                        return
        await self.app(scope, receive, send)
