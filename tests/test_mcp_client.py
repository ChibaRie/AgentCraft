"""MCP JSON-RPC 客户端测试（手册 §6.7/§6.8/§10.2：MCP 客户端逻辑保留在控制面 Python）。

- 协议层（协议版 2024-11-05，LF 分帧 JSON-RPC 2.0）：initialize →
  notifications/initialized → tools/list / tools/call；server isError=true
  以 {content, is_error=True} 返回；JSON-RPC error 帧 → MCPClientError
- stdio 传输：脚本化假进程；运行形态解析有 docker → mcp-sandbox 容器
  argv（env 经 -e 注入、命令经 sh -c），无 docker → 本地子进程（开发回退）
- 超时：单请求受 timeout 约束；结果 > 上限按 UTF-8 字节截断
- http 传输（streamable HTTP JSON-RPC）：session 头保持、json 应答解析
"""

import asyncio
import json

import httpx
import pytest

from backend.config import Settings
from backend.engine.mcp_client import (
    McpClient,
    MCPClientError,
    StdioTransport,
    resolve_stdio_argv,
    truncate_content,
)


def make_settings(**overrides) -> Settings:
    return Settings(
        HOST_DATA_ROOT="./data",
        HOST_WORKSPACE_ROOT="./workspaces",
        MCP_CALL_TIMEOUT_SECONDS=2,
        **overrides,
    )


# ---------------------------------------------------------------------------
# 假 stdio 进程：脚本化 JSON-RPC 应答，记录收到的请求行
# ---------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, responses: list[str], fail_write: bool = False) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.killed = False
        self.returncode: int | None = None
        self._fail_write = fail_write
        self.stdin = self  # 与 asyncio.Process 同形：stdin.write / stdout.readline
        self.stdout = self

    # stdin 端
    def write(self, data: bytes) -> int:
        if self._fail_write:
            raise BrokenPipeError("process gone")
        self.requests.append(json.loads(data.decode()))
        return len(data)

    async def drain(self) -> None:
        return None

    # stdout 端
    async def readline(self) -> bytes:
        if self._responses:
            return self._responses.pop(0).encode() + b"\n"
        return b""  # EOF

    async def wait(self) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeStdioTransport(StdioTransport):
    """注入假进程的 stdio 传输（不真正 spawn 子进程）。"""

    def __init__(self, process: FakeProcess) -> None:
        super().__init__(argv=["fake"], env={})
        self._process = process

    async def _spawn(self):
        return self._process


INIT_RESPONSE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fs", "version": "1.0"},
        },
    }
)
TOOLS_RESPONSE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "tools": [
                {
                    "name": "list_directory",
                    "description": "列出目录内容",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ]
        },
    }
)
CALL_RESPONSE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {
            "content": [
                {"type": "text", "text": "a.txt"},
                {"type": "text", "text": "b.txt"},
                {"type": "image", "data": "...", "mimeType": "image/png"},
            ],
            "isError": False,
        },
    }
)


def fs_client(process: FakeProcess) -> McpClient:
    return McpClient(FakeStdioTransport(process), timeout=2.0)


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------


async def test_stdio_discover_lists_tools():
    process = FakeProcess([INIT_RESPONSE, TOOLS_RESPONSE])
    async with fs_client(process) as client:
        tools = await client.discover()
    assert tools == [
        {
            "name": "list_directory",
            "description": "列出目录内容",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        }
    ]
    # 协议握手：initialize → notifications/initialized → tools/list
    assert process.requests[0]["method"] == "initialize"
    assert process.requests[0]["params"]["protocolVersion"]
    assert process.requests[1]["method"] == "notifications/initialized"
    assert process.requests[2]["method"] == "tools/list"
    assert process.killed  # with 块退出后进程被回收


async def test_stdio_call_joins_text_and_marks_image():
    process = FakeProcess([INIT_RESPONSE, CALL_RESPONSE])
    async with fs_client(process) as client:
        result = await client.call("list_directory", {"path": "/workspace"})
    assert result["is_error"] is False
    assert result["content"] == "a.txt\nb.txt\n[image]"


async def test_stdio_call_tool_error_returns_is_error():
    """server 返回 isError=true：不算传输失败，按 {content, is_error=True} 透传。"""
    err_response = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"content": [{"type": "text", "text": "路径不存在"}], "isError": True},
        }
    )
    process = FakeProcess([INIT_RESPONSE, err_response])
    async with fs_client(process) as client:
        result = await client.call("list_directory", {"path": "/nope"})
    assert result["is_error"] is True
    assert "路径不存在" in result["content"]


async def test_stdio_rpc_error_raises():
    """JSON-RPC error 帧（如未知工具）→ MCPClientError。"""
    err = json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -32602, "message": "bad"}})
    process = FakeProcess([INIT_RESPONSE, err])
    async with fs_client(process) as client:
        with pytest.raises(MCPClientError):
            await client.discover()


async def test_stdio_timeout_raises():
    """无应答 → 单请求超时 MCPClientError（不悬挂）。"""

    class SilentProcess(FakeProcess):
        async def readline(self) -> bytes:
            await asyncio.sleep(10)
            return b""

    process = SilentProcess([])
    async with McpClient(FakeStdioTransport(process), timeout=0.05) as client:
        with pytest.raises(MCPClientError, match="超时|timeout"):
            await client.discover()


async def test_stdio_write_failure_raises():
    """进程已死（write BrokenPipe）→ MCPClientError 而非裸异常。"""
    process = FakeProcess([], fail_write=True)
    async with fs_client(process) as client:
        with pytest.raises(MCPClientError):
            await client.discover()


# ---------------------------------------------------------------------------
# stdio 运行形态解析（沙箱容器 vs 开发回退）
# ---------------------------------------------------------------------------


def test_resolve_stdio_argv_uses_sandbox_image_with_env(monkeypatch):
    monkeypatch.setattr("backend.engine.mcp_client.shutil.which", lambda name: "C:/docker")
    settings = make_settings(
        MCP_SANDBOX_IMAGE="agentcraft-mcp-sandbox:test",
        PI_NETWORK_NAME="agentcraft-internal",
    )
    argv, env = resolve_stdio_argv(
        "mcp-server-fs /workspace", {"FS_ROOT": "/workspace"}, settings
    )
    assert argv[0] == "docker"
    assert "run" in argv and "--rm" in argv and "-i" in argv
    assert "agentcraft-mcp-sandbox:test" in argv
    assert "-e" in argv and "FS_ROOT=/workspace" in argv
    # 命令经 sh -c 进沙箱（host 侧无 shell 解释）；加入受限网络
    assert argv[-3] == "sh" and argv[-2] == "-c"
    assert argv[-1] == "mcp-server-fs /workspace"
    assert "agentcraft-internal" in argv
    assert env == {}  # env 已注入 argv，不再进子进程环境


def test_resolve_stdio_argv_falls_back_to_local_subprocess(monkeypatch):
    monkeypatch.setattr("backend.engine.mcp_client.shutil.which", lambda name: None)
    settings = make_settings()
    argv, env = resolve_stdio_argv(
        "mcp-server-fs  /workspace", {"FS_ROOT": "/workspace"}, settings
    )
    assert argv == ["mcp-server-fs", "/workspace"]
    assert env["FS_ROOT"] == "/workspace"  # env 经子进程环境注入


# ---------------------------------------------------------------------------
# 截断
# ---------------------------------------------------------------------------


def test_truncate_content_byte_budget():
    content = "汉" * 100000  # 每字 3 字节
    result = truncate_content(content, max_bytes=102400)
    assert len(result.encode("utf-8")) <= 102400
    assert result.endswith("…（结果超长已截断）")


def test_truncate_content_noop_under_limit():
    assert truncate_content("short", max_bytes=102400) == "short"


# ---------------------------------------------------------------------------
# http 传输（streamable HTTP JSON-RPC）
# ---------------------------------------------------------------------------


async def test_http_discover_and_call_keep_session():
    """streamable HTTP：initialize 应答的 Mcp-Session-Id 在后续请求中保持。"""
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_headers.append(request.headers)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json=json.loads(INIT_RESPONSE),
                headers={"content-type": "application/json", "mcp-session-id": "sess-123"},
            )
        if body.get("method") == "tools/list":
            return httpx.Response(
                200, json=json.loads(TOOLS_RESPONSE), headers={"content-type": "application/json"}
            )
        return httpx.Response(
            200, json=json.loads(CALL_RESPONSE), headers={"content-type": "application/json"}
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = McpClient.http("http://mcp-sandbox:3000/mcp", http_client=http_client)
    tools = await client.discover()
    assert tools[0]["name"] == "list_directory"
    # initialize 之后的请求带 session 头
    assert seen_headers[0].get("mcp-session-id") is None
    assert seen_headers[-1].get("mcp-session-id") == "sess-123"
    await http_client.aclose()


async def test_http_connection_error_raises():
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(unreachable))
    client = McpClient.http("http://mcp-sandbox:3000/mcp", http_client=http_client)
    with pytest.raises(MCPClientError):
        await client.discover()
    await http_client.aclose()
