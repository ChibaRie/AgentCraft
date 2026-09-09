"""MCP JSON-RPC 客户端（手册 §6.7/§6.8/§10.2：MCP 客户端逻辑保留在控制面 Python）。

- 协议：MCP 2024-11-05（JSON-RPC 2.0）。stdio 传输按 LF 分帧；http 传输
  走 streamable HTTP（POST 单端点，保持 Mcp-Session-Id，兼容 json / SSE 应答）
- stdio 运行形态（resolve_stdio_argv）：有 docker → `docker run --rm -i`
  mcp-sandbox 沙箱容器（env 经 -e 注入、命令经 sh -c 进入受限网络）；
  无 docker → 本地子进程（开发回退，无沙箱）
- 纪律：env 值（可能含密钥）只进 argv/子进程环境，绝不落日志；每次业务
  调用建一次性客户端（连接-调用-关闭），超时按单请求计
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil

import httpx

from backend.config import Settings
from backend.engine.docker_transport import MAX_LINE_BYTES

logger = logging.getLogger("agentcraft")

_MCP_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "agentcraft", "version": "1.0"}


class MCPClientError(Exception):
    """MCP 连接/协议/超时类失败（API 层转 502；不携带 env 密文或密钥）。"""


def truncate_content(content: str, *, max_bytes: int) -> str:
    """结果超限按 UTF-8 字节截断并加标记（§6.8：结果 > 100KB 截断）。"""
    encoded = content.encode("utf-8")
    marker = "…（结果超长已截断）"
    if len(encoded) + len(marker.encode("utf-8")) <= max_bytes:
        return content
    clipped = encoded[: max_bytes - len(marker.encode("utf-8"))].decode("utf-8", errors="ignore")
    return clipped + marker


def resolve_stdio_argv(
    command: str, env: dict[str, str], settings: Settings
) -> tuple[list[str], dict[str, str]]:
    """解析 stdio 启动形态，返回 (argv, subprocess_env)。

    沙箱形态：env 以 -e 注入容器（argv 含密钥值，禁止日志输出 argv）；
    开发回退：命令 shlex 切分本地直跑，env 并入子进程环境（无沙箱，仅开发）。
    """
    if shutil.which("docker"):
        argv = ["docker", "run", "--rm", "-i", "--network", settings.PI_NETWORK_NAME]
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [settings.MCP_SANDBOX_IMAGE, "sh", "-c", command]
        return argv, {}
    return shlex.split(command), dict(env)


class StdioTransport:
    """LF 分帧 JSON-RPC stdio 传输（子进程由 _spawn 生产，测试可注入）。"""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self._argv = argv
        self._env = env
        self._process = None

    async def _spawn(self):
        # limit 与 Pi 传输层同源（32MiB 共享常量）：MCP 大结果以单行 JSON 回传，
        # asyncio 默认 64KB 行上限会在传输层先崩（v0.12.3 同源缺陷）
        return await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**_host_env(), **self._env} if self._env else None,
            limit=MAX_LINE_BYTES,
        )

    async def request(self, payload: dict) -> dict | None:
        if self._process is None:
            try:
                self._process = await self._spawn()
            except OSError as exc:
                raise MCPClientError(f"MCP Server 启动失败: {type(exc).__name__}") from exc
        body = json.dumps(payload, ensure_ascii=False).encode()
        try:
            self._process.stdin.write(body + b"\n")
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise MCPClientError("MCP Server 进程已退出") from exc
        if "id" not in payload:
            return None  # 通知不等待应答
        try:
            line = await self._process.stdout.readline()
        except ValueError as exc:
            # MCP 是 1:1 请求应答：超限帧不能像 Pi 传输那样丢弃（跳过即挂死等待方），
            # 必须显式报错由上层转 502
            raise MCPClientError("MCP Server 应答超过单行上限") from exc
        if not line:
            raise MCPClientError("MCP Server 连接已关闭")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise MCPClientError("MCP Server 应答不是合法 JSON") from exc

    async def close(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()

    @property
    def description(self) -> str:
        return f"stdio[{self._argv[0]}]"


class HttpTransport:
    """streamable HTTP JSON-RPC 传输（单端点 POST，session 头保持）。"""

    def __init__(self, url: str, http_client: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._client = http_client
        self._owns_client = http_client is None
        self._session_id: str | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def request(self, payload: dict) -> dict | None:
        headers = {"Accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            response = await self._ensure_client().post(self._url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise MCPClientError(f"MCP Server 连接失败: {type(exc).__name__}") from exc
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        if response.status_code >= 400:
            raise MCPClientError(f"MCP Server HTTP {response.status_code}")
        if response.status_code == 202 or not response.content:
            return None  # 通知受理（202 Accepted）
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return _parse_sse_response(response.text, payload.get("id"))
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise MCPClientError("MCP Server 应答不是合法 JSON") from exc

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def description(self) -> str:
        return f"http[{self._url}]"


def _parse_sse_response(body: str, request_id: int | str | None) -> dict | None:
    """从 SSE 文本中提取与本请求 id 匹配的 JSON-RPC 应答。"""
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise MCPClientError("MCP Server SSE 流中无匹配应答")


def _host_env() -> dict[str, str]:
    """开发回退形态下子进程需要的宿主环境（PATH 等；不含任何密钥）。"""
    return dict(os.environ)


class McpClient:
    """一次性 MCP 客户端：握手 → 单次操作 → 关闭（async with 管理）。"""

    def __init__(self, transport, *, timeout: float, max_result_bytes: int = 102_400) -> None:
        self._transport = transport
        self._timeout = timeout
        self._max_result_bytes = max_result_bytes
        self._next_id = 0
        self._initialized = False

    @classmethod
    def stdio(cls, command: str, env: dict[str, str], settings: Settings) -> "McpClient":
        argv, subprocess_env = resolve_stdio_argv(command, env, settings)
        transport = StdioTransport(argv, subprocess_env)
        return cls(
            transport,
            timeout=float(settings.MCP_CALL_TIMEOUT_SECONDS),
            max_result_bytes=settings.MCP_RESULT_MAX_BYTES,
        )

    @classmethod
    def http(
        cls,
        url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        max_result_bytes: int = 102_400,
    ) -> "McpClient":
        return cls(
            HttpTransport(url, http_client),
            timeout=timeout,
            max_result_bytes=max_result_bytes,
        )

    async def __aenter__(self) -> "McpClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def close(self) -> None:
        await self._transport.close()

    async def discover(self) -> list[dict]:
        """tools/list：返回 [{name, description, input_schema}]。"""
        await self._initialize()
        result = await self._request("tools/list")
        tools = []
        for item in result.get("tools", []):
            tools.append(
                {
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "input_schema": item.get("inputSchema") or {"type": "object"},
                }
            )
        return tools

    async def call(self, name: str, arguments: dict) -> dict:
        """tools/call：返回 {content, is_error}；server isError 属工具结果非传输失败。"""
        await self._initialize()
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        content = _content_to_text(result.get("content", []))
        is_error = bool(result.get("isError"))
        return {
            "content": truncate_content(content, max_bytes=self._max_result_bytes),
            "is_error": is_error,
        }

    async def _initialize(self) -> None:
        if self._initialized:
            return
        await self._request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        await self._notify("notifications/initialized")
        self._initialized = True

    async def _notify(self, method: str) -> None:
        await self._transport.request({"jsonrpc": "2.0", "method": method})

    async def _request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        payload: dict = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            message = await asyncio.wait_for(self._transport.request(payload), self._timeout)
        except asyncio.TimeoutError as exc:
            raise MCPClientError(f"MCP 请求超时（{method}）") from exc
        if message is None:
            raise MCPClientError(f"MCP 请求无应答（{method}）")
        if "error" in message:
            detail = message["error"].get("message", "unknown")
            raise MCPClientError(f"MCP 请求失败（{method}）: {detail}")
        return message.get("result") or {}


def _content_to_text(blocks: list) -> str:
    """MCP content 数组 → 文本（text 连接；非文本块以占位符标注）。"""
    parts = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "image":
            parts.append("[image]")
        elif block.get("type") == "resource":
            parts.append("[resource]")
    return "\n".join(part for part in parts if part)
