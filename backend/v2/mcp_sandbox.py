"""用户 MCP stdio 沙箱执行链（Phase 10 M5）。

安全底线（不可谈判项，Phase 10 设计 §一）：
- 用户注册的 stdio 命令**只在一次性 mcp-sandbox 容器内执行**，控制面宿主机
  零接触；
- 容器只读 rootfs、cap_drop=ALL、no-new-privileges、仅 internal 网络、零宿主
  挂载（与 pi-worker 同款沙箱清单，ContainerSpec 唯一事实源）；
- 启动材料 {command, args, env} 由调用方解封后经本模块组装成容器 argv/env，
  明文只在本协程内存存活，绝不落日志/审计/错误消息。

传输形态：每次发现或每次 tools/call 拉起一个 ``docker run`` 一次性容器，
在该容器内完成 MCP stdio 握手（initialize → notifications/initialized →
tools/list 或 tools/call）后立即销毁——与 V1 McpClient 一致，无跨调用状态。

传输通道：优先 Docker API（compose 形态经 docker-socket-proxy，DOCKER_API_URL），
无 API 时回退 docker CLI（开发机 Docker Desktop，npipe）；两条通道共用
``ContainerSpec`` 安全清单，不得漂移。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil

from backend.config import Settings, get_settings
from backend.engine.docker_transport import (
    ContainerSpec,
    DockerApiTransport,
    DockerCliTransport,
    docker_ensure_network,
)

logger = logging.getLogger("agentcraft.mcp.sandbox")

MCP_PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "agentcraft", "version": "1.0"}

_SANDBOX_USER = "mcpworker"
_SANDBOX_WORKDIR = "/workspace"
# 只读 rootfs 的唯一可写点；MCP server 自身需要的缓存/HOME 均落 tmpfs
_SANDBOX_TMPFS = {
    "/tmp": "rw,size=64m,nosuid,nodev,noexec",
    "/home/mcpworker": "rw,size=32m,nosuid,nodev,noexec",
}

_MAX_OUTPUT_BYTES = 2 * 1024 * 1024


class McpSandboxError(Exception):
    """沙箱启动/协议/超时类失败（API 层统一转 502，不带任何材料细节）。"""


def validate_launch(launch: dict) -> dict:
    """启动材料形态校验（服务层已校验一次，此处为执行面防御）。

    返回归一化后的 {command, args, env}；任何非法形态抛 McpSandboxError。
    绝不把材料内容放进异常消息（红线）。
    """
    if not isinstance(launch, dict):
        raise McpSandboxError("非法 MCP 启动材料")
    command = launch.get("command")
    args = launch.get("args")
    env = launch.get("env")
    if not isinstance(command, str) or not command or "\x00" in command:
        raise McpSandboxError("非法 MCP 启动材料")
    if args is None:
        args = []
    if not isinstance(args, list) or not all(isinstance(a, str) and "\x00" not in a for a in args):
        raise McpSandboxError("非法 MCP 启动材料")
    if env is None:
        env = {}
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) and "\x00" not in k + v
        for k, v in env.items()
    ):
        raise McpSandboxError("非法 MCP 启动材料")
    return {"command": command, "args": list(args), "env": dict(env)}


def build_sandbox_spec(
    *,
    image: str,
    network_name: str,
    launch: dict,
    task_id: str,
    server_id: str,
) -> ContainerSpec:
    """把解封后的启动材料组装为一次性沙箱容器规格（纯函数，测试直测）。

    argv = [command, *args] 经 Docker API/CLI 数组直传，绝不经 shell 拼接；
    env 为用户声明的键值（值可能含密钥，只进容器 env，禁日志）。
    """
    if not image:
        raise McpSandboxError("mcp-sandbox 镜像未配置")
    material = validate_launch(launch)
    session = secrets.token_hex(4)
    return ContainerSpec(
        container_name=f"mcp-sandbox-{task_id}-{server_id[:8]}-{session}",
        image=image,
        argv=[material["command"], *material["args"]],
        env=material["env"],
        mounts=[],
        network_name=network_name,
        labels={
            "agentcraft.component": "mcp-sandbox",
            "agentcraft.task_id": str(task_id),
            "agentcraft.mcp_server_id": str(server_id),
        },
        workdir=_SANDBOX_WORKDIR,
        user=_SANDBOX_USER,
        tmpfs=dict(_SANDBOX_TMPFS),
    )


class SandboxStdioSession:
    """一次性容器内 MCP stdio 会话（握手 + 单次操作 + 销毁）。"""

    def __init__(
        self,
        transport,
        *,
        timeout: float,
        max_output_bytes: int = _MAX_OUTPUT_BYTES,
    ) -> None:
        self._transport = transport
        self._timeout = float(timeout)
        self._max_output_bytes = int(max_output_bytes)
        self._next_id = 0
        self._initialized = False

    async def __aenter__(self) -> "SandboxStdioSession":
        try:
            await self._transport.start()
        except Exception as exc:  # noqa: BLE001 - 启动失败统一沙箱错误（零细节）
            raise McpSandboxError("MCP 沙箱启动失败") from exc
        return self

    async def __aexit__(self, *exc_info) -> None:
        try:
            await self._transport.close()
        except Exception:  # noqa: BLE001 - 收尾尽力而为
            logger.warning("MCP 沙箱容器回收失败（材料不入日志）")
        # API 通道 close() 只关 attach 流不删容器（ContainerSpec 与 pi-worker
        # 共用，AutoRemove=False）；一次性沙箱必须显式 force delete。CLI 通道
        # 以 --rm 自清，无 _container 属性，本段自然跳过。
        container = getattr(self._transport, "_container", None)
        if container is not None:
            try:
                await container.delete(force=True)
            except Exception:  # noqa: BLE001 - 容器可能已退出/自删
                pass

    async def _send(self, payload: dict) -> None:
        await self._transport.write_line(json.dumps(payload, ensure_ascii=False))

    async def _recv(self, want_id: int) -> dict:
        received = 0
        while True:
            line = await asyncio.wait_for(self._transport.readline(), self._timeout)
            if not line:
                raise McpSandboxError("MCP 沙箱连接已关闭")
            received += len(line.encode("utf-8"))
            if received > self._max_output_bytes:
                raise McpSandboxError("MCP 沙箱输出超限")
            try:
                message = json.loads(line)
            except ValueError:
                continue  # server 日志/通知行：跳过
            if isinstance(message, dict) and message.get("id") == want_id:
                return message

    async def _request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        payload: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._send(payload)
            message = await self._recv(request_id)
        except asyncio.TimeoutError as exc:
            raise McpSandboxError("MCP 沙箱请求超时") from exc
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise McpSandboxError("MCP 沙箱通道已断") from exc
        if "error" in message:
            raise McpSandboxError("MCP server 返回错误")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def _initialize(self) -> None:
        if self._initialized:
            return
        await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    async def list_tools(self) -> list:
        await self._initialize()
        result = await self._request("tools/list")
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list):
            raise McpSandboxError("MCP tools/list 响应形态非法")
        return raw_tools

    async def call_tool(self, name: str, arguments: dict, *, max_result_bytes: int) -> dict:
        await self._initialize()
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        content = _content_to_text(result.get("content"))
        return {
            "content": _truncate(content, max_result_bytes),
            "is_error": bool(result.get("isError")),
        }


def _content_to_text(blocks) -> str:
    """MCP content 数组 → 文本（非文本块以占位符标注，与 V1 同型）。"""
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif block.get("type") == "image":
            parts.append("[image]")
        elif block.get("type") == "resource":
            parts.append("[resource]")
    return "\n".join(part for part in parts if part)


def _truncate(content: str, max_bytes: int) -> str:
    """UTF-8 字节截断 + 标记（结果超限保护）。"""
    encoded = content.encode("utf-8")
    marker = "…（结果超长已截断）"
    if len(encoded) <= max_bytes:
        return content
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    return encoded[:budget].decode("utf-8", errors="ignore") + marker


# ---------------------------------------------------------------------------
# 传输通道选择（API 优先 / CLI 回退；两通道共用 ContainerSpec 安全清单）
# ---------------------------------------------------------------------------


async def _try_api_transport(spec: ContainerSpec, settings: Settings):
    """Docker API 通道（compose 经 docker-socket-proxy）。探测失败返回 None。"""
    docker = None
    try:
        import aiodocker

        docker = aiodocker.Docker(url=settings.DOCKER_API_URL)
        await docker.system.info()
    except Exception:  # noqa: BLE001 - 探测失败即回退
        if docker is not None:
            try:
                await docker.close()
            except Exception:  # noqa: BLE001
                pass
        return None
    return DockerApiTransport(docker, spec)


async def _open_transport(spec: ContainerSpec, settings: Settings):
    """按 API → CLI 顺序选择容器通道；两者都不可用 → McpSandboxError。"""
    transport = await _try_api_transport(spec, settings)
    if transport is not None:
        return transport
    if shutil.which("docker"):
        try:
            await docker_ensure_network(spec.network_name)
        except Exception:  # noqa: BLE001 - 网络已存在等形态不阻断
            pass
        return DockerCliTransport(spec)
    raise McpSandboxError("MCP 沙箱运行通道不可用")


async def open_stdio_session(
    *,
    image: str,
    network_name: str,
    launch: dict,
    task_id: str,
    server_id: str,
    timeout: float,
    max_output_bytes: int = _MAX_OUTPUT_BYTES,
    settings: Settings | None = None,
) -> SandboxStdioSession:
    """拉起一次性 mcp-sandbox 容器并返回 stdio 会话（调用方 async with 收尾）。"""
    settings = settings or get_settings()
    spec = build_sandbox_spec(
        image=image,
        network_name=network_name,
        launch=launch,
        task_id=task_id,
        server_id=server_id,
    )
    transport = await _open_transport(spec, settings)
    return SandboxStdioSession(transport, timeout=timeout, max_output_bytes=max_output_bytes)
