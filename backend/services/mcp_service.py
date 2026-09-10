"""MCP 管理业务逻辑（手册 §6.7/§11.3、DB 设计 §3.9-§3.11）。

- Server CRUD：transport 条件必填（stdio→command、http-sse→url）；
  env_vars 以 AES-256-GCM 信封落库（AAD 绑定 server_id），API 只回变量名
- discover：tools/list 落库；新工具默认 sensitive=1 + enabled=0（保守判定，
  仅可信只读 allowlist 置非敏感）；重复 discover 更新描述/schema 并保留状态
- 工具开关：直接生效；敏感工具启用时记录授权时点 authorized_at，禁用清空
- publish 需 ≥1 已发现工具；offline 仅限 published；删除需无绑定且无
  tasks.mcp_snapshot 引用（409；可先下架立即止损）
- 专家绑定：owner 归属一致、Server 必须 published、UNIQUE(expert, server)
- 快照装配 load_snapshot_tools：enabled 绑定 ∩ published Server ∩ enabled
  工具 ∩（非敏感 或 已授权）——任务创建时冻结为能力上限（§12 决策 15）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.engine.mcp_client import McpClient, MCPClientError
from backend.models.expert import Expert
from backend.models.expert_mcp import ExpertMCP
from backend.models.mcp_server import MCPServer
from backend.models.mcp_tool import MCPTool
from backend.models.task import Task
from backend.services.user_service import UserSystemError
from backend.utils.crypto import (
    EncryptionError,
    decrypt_text,
    encrypt_text,
    make_keyring,
    mcp_env_aad,
)

logger = logging.getLogger("agentcraft")

_TRANSPORTS = ("stdio", "http-sse")

# 可信只读工具 allowlist（§6.7：仅系统能判定的可信只读工具可置非敏感；
# Server 声明/名称/描述均不能降级敏感）
_TRUSTED_READONLY_TOOLS = frozenset(
    {
        "list_directory",
        "directory_tree",
        "read_file",
        "read_text_file",
        "read_multiple_files",
        "search_files",
        "get_file_info",
        "list_allowed_directories",
        "get_current_time",
    }
)


class MCPServerNotFoundError(UserSystemError):
    status_code = 404
    code = "NOT_FOUND"


class MCPServerInvalidError(UserSystemError):
    status_code = 400
    code = "INVALID_SERVER"


class MCPServerReferencedError(UserSystemError):
    status_code = 409
    code = "SERVER_STILL_REFERENCED"


class MCPAlreadyBoundError(UserSystemError):
    status_code = 409
    code = "ALREADY_BOUND"


class MCPBindingNotFoundError(UserSystemError):
    status_code = 404
    code = "NOT_FOUND"


class MCPUpstreamError(UserSystemError):
    status_code = 502
    code = "MCP_UPSTREAM_FAILED"


class EncryptionUnavailableError(UserSystemError):
    status_code = 503
    code = "ENCRYPTION_UNCONFIGURED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _keyring(settings: Settings) -> tuple[str, dict[str, bytes]]:
    try:
        return make_keyring(
            settings.MCP_ENCRYPTION_KEYRING, active_kid=settings.MCP_ENCRYPTION_ACTIVE_KID
        )
    except EncryptionError as exc:
        raise EncryptionUnavailableError(str(exc)) from exc


def _validate_transport_fields(transport: str, command: str | None, url: str | None) -> None:
    if transport not in _TRANSPORTS:
        raise MCPServerInvalidError("transport 仅支持 stdio / http-sse")
    if transport == "stdio":
        if not (command or "").strip():
            raise MCPServerInvalidError("transport=stdio 时 command 必填")
        _validate_env_keys(command)
    if transport == "http-sse":
        if not (url or "").strip():
            raise MCPServerInvalidError("transport=http-sse 时 url 必填")
        _validate_mcp_url(url)


def _validate_mcp_url(url: str) -> None:
    """http-sse 端点 URL 校验（scheme + 主机名必填 + 禁凭据内嵌）。

    SSRF 立场与 provider base_url（§7.7）一致：本地单操作者部署，
    Ollama/内网 mcp-sandbox 等私网端点是合法目标，不封禁私网/回环。
    """
    from urllib.parse import urlparse

    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise MCPServerInvalidError("url 必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise MCPServerInvalidError("url 缺少主机名")
    if parsed.username or parsed.password:
        raise MCPServerInvalidError("url 不允许内嵌凭据")


def _validate_env_keys(command: str) -> None:
    """stdio 命令沙箱语义说明（§6.7 设计如此）：注册 stdio Server 即授权
    在 mcp-sandbox 沙箱内执行该命令（无挂载、非 root、internal 网络）——
    与 Claude Code 等本地编码代理注册 MCP Server 的语义一致。env 变量名
    校验防止经 -e 注入拼接歧义（值不限）。"""
    return None


def _validate_env_var_name(key: str) -> None:
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key or ""):
        raise MCPServerInvalidError(f"环境变量名不合法: {key[:20]}")


def _encrypt_env(env_vars: dict[str, str] | None, server_id: int, settings: Settings) -> str | None:
    if not env_vars:
        return None
    active_kid, keyring = _keyring(settings)
    try:
        envelope = encrypt_text(
            json.dumps(env_vars, ensure_ascii=False),
            aad=mcp_env_aad(server_id),
            keyring=keyring,
            active_kid=active_kid,
        )
    except EncryptionError as exc:
        raise EncryptionUnavailableError(str(exc)) from exc
    return json.dumps(envelope, ensure_ascii=False)


def decrypt_server_env(server: MCPServer, settings: Settings) -> dict[str, str]:
    """运行期解密 env_vars（仅内存使用，不落日志；信封损坏 → 503）。"""
    if not server.env_vars:
        return {}
    _, keyring = _keyring(settings)
    try:
        plaintext = decrypt_text(
            json.loads(server.env_vars), aad=mcp_env_aad(server.id), keyring=keyring
        )
    except (EncryptionError, json.JSONDecodeError) as exc:
        raise EncryptionUnavailableError("MCP Server env 解密失败") from exc
    try:
        env = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise EncryptionUnavailableError("MCP Server env 明文不是合法 JSON") from exc
    return env if isinstance(env, dict) else {}


def list_env_var_names(server: MCPServer, settings: Settings) -> list[str]:
    """env 变量名列表（变量名非机密；解密失败一律空列表，不回密文）。"""
    if not server.env_vars:
        return []
    try:
        env = decrypt_server_env(server, settings)
    except EncryptionUnavailableError:
        return []
    return sorted(env.keys())


def server_payload(
    server: MCPServer, *, settings: Settings | None = None, with_tools: list[dict] | None = None
) -> dict:
    """API 响应构造：env 只回变量名，永不回密文/明文（§11.3）。"""
    payload: dict = {
        "id": server.id,
        "name": server.name,
        "description": server.description,
        "transport": server.transport,
        "url": server.url,
        "command": server.command,
        "env_var_names": list_env_var_names(server, settings) if settings else [],
        "status": server.status,
        "created_at": server.created_at,
        "updated_at": server.updated_at,
    }
    if with_tools is not None:
        payload["tools"] = with_tools
    return payload


def tool_payload(tool: MCPTool) -> dict:
    return {
        "id": tool.id,
        "name": tool.name,
        "description": tool.description,
        "input_schema": json.loads(tool.input_schema) if tool.input_schema else {},
        "sensitive": bool(tool.sensitive),
        "enabled": bool(tool.enabled),
        "authorized_at": tool.authorized_at,
    }


async def _get_owned_server(db: AsyncSession, owner_id: int, server_id: int) -> MCPServer:
    """统一 404（不区分不存在/非本人，与 Provider/任务侧口径一致）。"""
    server = await db.get(MCPServer, server_id)
    if server is None or server.owner_id != owner_id:
        raise MCPServerNotFoundError("MCP Server 不存在")
    return server


async def create_server(
    db: AsyncSession,
    owner_id: int,
    *,
    name: str,
    description: str,
    transport: str,
    command: str | None = None,
    url: str | None = None,
    env_vars: dict[str, str] | None = None,
    settings: Settings,
) -> MCPServer:
    _validate_transport_fields(transport, command, url)
    for key in env_vars or {}:
        _validate_env_var_name(key)
    server = MCPServer(
        owner_id=owner_id,
        name=name,
        description=description,
        transport=transport,
        url=url,
        command=command,
        status="draft",
    )
    db.add(server)
    await db.flush()  # 先取 id：信封 AAD 绑定 server_id
    server.env_vars = _encrypt_env(env_vars, server.id, settings)
    await db.commit()
    await db.refresh(server)
    return server


async def list_servers(
    db: AsyncSession, owner_id: int, page: int, size: int
) -> tuple[list[MCPServer], int]:
    total = (
        await db.execute(
            select(func.count()).select_from(MCPServer).where(MCPServer.owner_id == owner_id)
        )
    ).scalar_one()
    result = await db.execute(
        select(MCPServer)
        .where(MCPServer.owner_id == owner_id)
        .order_by(MCPServer.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(result.scalars()), int(total)


async def get_server_detail(
    db: AsyncSession, owner_id: int, server_id: int
) -> tuple[MCPServer, list[MCPTool]]:
    server = await _get_owned_server(db, owner_id, server_id)
    tools = list(
        (
            await db.execute(
                select(MCPTool).where(MCPTool.server_id == server_id).order_by(MCPTool.id)
            )
        ).scalars()
    )
    return server, tools


async def update_server(
    db: AsyncSession,
    owner_id: int,
    server_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    command: str | None = None,
    url: str | None = None,
    env_vars: dict[str, str] | None = None,
    env_vars_provided: bool = False,
    settings: Settings | None = None,
) -> MCPServer:
    server = await _get_owned_server(db, owner_id, server_id)
    if name is not None:
        server.name = name
    if description is not None:
        server.description = description
    if command is not None:
        server.command = command
    if url is not None:
        server.url = url
    if env_vars_provided:
        assert settings is not None
        for key in env_vars or {}:
            _validate_env_var_name(key)
        server.env_vars = _encrypt_env(env_vars, server.id, settings)
    _validate_transport_fields(server.transport, server.command, server.url)
    server.updated_at = _now()
    await db.commit()
    await db.refresh(server)
    return server


def _default_client_factory(settings: Settings):
    """真实客户端工厂：stdio 解密 env 进沙箱；http 直连端点。"""

    def factory(server: MCPServer) -> McpClient:
        if server.transport == "stdio":
            env = decrypt_server_env(server, settings)
            return McpClient.stdio(server.command or "", env, settings)
        return McpClient.http(server.url or "")

    return factory


async def discover_tools(
    db: AsyncSession,
    owner_id: int,
    server_id: int,
    settings: Settings,
    client_factory=None,
) -> list[dict]:
    """连接 Server 执行 tools/list 并落库（§6.7）；连接失败 → 502。"""
    server = await _get_owned_server(db, owner_id, server_id)
    factory = client_factory or _default_client_factory(settings)
    client = factory(server)
    try:
        discovered = await client.discover()
    except MCPClientError as exc:
        # 详情只进日志：错误文本可能来自用户注册的 Server，不回显前端
        logger.warning("discover server=%s 失败: %s", server_id, exc)
        raise MCPUpstreamError("连接 MCP Server 失败，请检查端点或命令后重试") from exc
    finally:
        await client.close()

    existing = {
        row.name: row
        for row in (
            await db.execute(select(MCPTool).where(MCPTool.server_id == server_id))
        ).scalars()
    }
    for entry in discovered:
        schema_json = json.dumps(
            entry.get("input_schema") or {"type": "object"}, ensure_ascii=False
        )
        row = existing.get(entry["name"])
        if row is not None:
            row.description = entry.get("description", "")
            row.input_schema = schema_json  # sensitive/enabled/authorized_at 保留
        else:
            db.add(
                MCPTool(
                    server_id=server_id,
                    name=entry["name"],
                    description=entry.get("description", ""),
                    input_schema=schema_json,
                    sensitive=entry["name"] not in _TRUSTED_READONLY_TOOLS,
                    enabled=False,
                )
            )
    server.updated_at = _now()
    await db.commit()
    return await list_tools(db, owner_id, server_id)


async def list_tools(db: AsyncSession, owner_id: int, server_id: int) -> list[dict]:
    _server, tools = await get_server_detail(db, owner_id, server_id)
    return [tool_payload(tool) for tool in tools]


async def update_tool(
    db: AsyncSession,
    owner_id: int,
    server_id: int,
    tool_id: int,
    *,
    enabled: bool,
) -> MCPTool:
    """工具开关（Server 全局，影响所有绑定专家的后续任务，§6.7）。

    敏感工具启用时直接记录授权时点（authorized_at）；禁用即撤销授权。
    """
    await _get_owned_server(db, owner_id, server_id)
    tool = await db.get(MCPTool, tool_id)
    if tool is None or tool.server_id != server_id:
        raise MCPServerNotFoundError("工具不存在")
    if enabled:
        if tool.sensitive:
            tool.authorized_at = _now()
        tool.enabled = True
    else:
        tool.enabled = False
        tool.authorized_at = None  # 禁用即撤销授权
    await db.commit()
    await db.refresh(tool)
    return tool


async def publish_server(db: AsyncSession, owner_id: int, server_id: int) -> MCPServer:
    server = await _get_owned_server(db, owner_id, server_id)
    if server.status == "published":
        raise MCPServerInvalidError("Server 已处于发布状态")
    tool_count = (
        await db.execute(
            select(func.count()).select_from(MCPTool).where(MCPTool.server_id == server_id)
        )
    ).scalar_one()
    if int(tool_count or 0) < 1:
        raise MCPServerInvalidError("发布条件不满足：需先连接并发现至少一个工具")
    server.status = "published"
    server.updated_at = _now()
    await db.commit()
    await db.refresh(server)
    return server


async def offline_server(db: AsyncSession, owner_id: int, server_id: int) -> MCPServer:
    server = await _get_owned_server(db, owner_id, server_id)
    if server.status != "published":
        raise MCPServerInvalidError("只有已发布的 Server 可以下架")
    server.status = "offline"
    server.updated_at = _now()
    await db.commit()
    await db.refresh(server)
    return server


async def delete_server(db: AsyncSession, owner_id: int, server_id: int) -> None:
    await _get_owned_server(db, owner_id, server_id)
    binding_count = (
        await db.execute(
            select(func.count()).select_from(ExpertMCP).where(ExpertMCP.server_id == server_id)
        )
    ).scalar_one()
    if int(binding_count or 0) > 0:
        raise MCPServerReferencedError("仍有专家绑定该 Server，请先解绑")
    # tasks.mcp_snapshot 引用检查（json 序列化两种冒号分隔形态都防）
    referenced = (
        await db.execute(
            select(func.count())
            .select_from(Task)
            .where(
                or_(
                    Task.mcp_snapshot.like(f'%"serverId": {server_id}%'),
                    Task.mcp_snapshot.like(f'%"serverId":{server_id}%'),
                )
            )
        )
    ).scalar_one()
    if int(referenced or 0) > 0:
        raise MCPServerReferencedError("仍有任务快照引用该 Server；可先下架立即阻断调用")
    server = await db.get(MCPServer, server_id)
    await db.delete(server)
    await db.commit()


# ---------------------------------------------------------------------------
# 专家绑定（§6.7 /api/experts/{id}/mcp）
# ---------------------------------------------------------------------------


async def _get_owned_expert(db: AsyncSession, owner_id: int, expert_id: int) -> Expert:
    expert = await db.get(Expert, expert_id)
    if expert is None or expert.owner_id != owner_id:
        raise MCPServerNotFoundError("专家不存在")
    return expert


async def bind_server(
    db: AsyncSession, owner_id: int, expert_id: int, server_id: int, *, enabled: bool
) -> ExpertMCP:
    await _get_owned_expert(db, owner_id, expert_id)
    server = await db.get(MCPServer, server_id)
    # 归属校验：他人 Server 统一 404（不暴露存在性）
    if server is None or server.owner_id != owner_id:
        raise MCPServerNotFoundError("MCP Server 不存在")
    if server.status != "published":
        raise MCPServerInvalidError("只有已发布的 Server 可以绑定专家")
    existing = (
        await db.execute(
            select(ExpertMCP).where(
                ExpertMCP.expert_id == expert_id, ExpertMCP.server_id == server_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise MCPAlreadyBoundError("该 Server 已绑定到此专家")
    binding = ExpertMCP(expert_id=expert_id, server_id=server_id, enabled=enabled)
    db.add(binding)
    await db.commit()
    await db.refresh(binding)
    return binding


async def update_binding(
    db: AsyncSession, owner_id: int, expert_id: int, server_id: int, *, enabled: bool
) -> ExpertMCP:
    await _get_owned_expert(db, owner_id, expert_id)
    binding = (
        await db.execute(
            select(ExpertMCP).where(
                ExpertMCP.expert_id == expert_id, ExpertMCP.server_id == server_id
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        raise MCPBindingNotFoundError("绑定关系不存在")
    binding.enabled = enabled
    await db.commit()
    await db.refresh(binding)
    return binding


async def unbind_server(db: AsyncSession, owner_id: int, expert_id: int, server_id: int) -> None:
    await _get_owned_expert(db, owner_id, expert_id)
    binding = (
        await db.execute(
            select(ExpertMCP).where(
                ExpertMCP.expert_id == expert_id, ExpertMCP.server_id == server_id
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        raise MCPBindingNotFoundError("绑定关系不存在")
    await db.delete(binding)
    await db.commit()


async def list_expert_bindings(db: AsyncSession, owner_id: int, expert_id: int) -> list[dict]:
    """专家详情 MCP 绑定列表（连接信息不返回，§6.3）。"""
    await _get_owned_expert(db, owner_id, expert_id)
    rows = (
        await db.execute(
            select(ExpertMCP, MCPServer)
            .join(MCPServer, MCPServer.id == ExpertMCP.server_id)
            .where(ExpertMCP.expert_id == expert_id)
            .order_by(ExpertMCP.id)
        )
    ).all()
    return [
        {
            "id": server.id,
            "name": server.name,
            "status": server.status,
            "enabled": bool(binding.enabled),
        }
        for binding, server in rows
    ]


# ---------------------------------------------------------------------------
# 任务快照装配（§12 决策 15：能力上限）与运行期校验/执行（§6.8）
# ---------------------------------------------------------------------------


class MCPKillSwitchError(UserSystemError):
    status_code = 403
    code = "MCP_TOOL_BLOCKED"


def _parse_snapshot_dt(value: str | None) -> datetime | None:
    """快照授权时点解析；统一转 naive UTC 以和 SQLite DATETIME 比较。"""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


async def validate_task_tool_call(
    db: AsyncSession, task: Task, server_id: int, tool_name: str
) -> None:
    """快照能力上限 + kill switch 双层校验（§6.8），失败即拒绝。

    - 上限：工具必须存在于 tasks.mcp_snapshot（否则任务运行时不可能见过它）
    - 实时：Server published、工具 enabled、专家绑定 enabled、敏感工具
      authorized_at 非空且不早于快照授权时点
    """
    snapshot_tools = json.loads(task.mcp_snapshot or "{}").get("tools", [])
    entry = next(
        (
            item
            for item in snapshot_tools
            if item.get("name") == tool_name and item.get("serverId") == server_id
        ),
        None,
    )
    if entry is None:
        raise MCPServerNotFoundError("工具不存在或未绑定到该任务")
    server = await db.get(MCPServer, server_id)
    if server is None or server.status != "published":
        raise MCPKillSwitchError("MCP Server 已下架，调用被阻断")
    tool = (
        await db.execute(
            select(MCPTool).where(MCPTool.server_id == server_id, MCPTool.name == tool_name)
        )
    ).scalar_one_or_none()
    if tool is None or not tool.enabled:
        raise MCPKillSwitchError("工具已禁用，调用被阻断")
    binding = (
        await db.execute(
            select(ExpertMCP).where(
                ExpertMCP.expert_id == task.expert_id, ExpertMCP.server_id == server_id
            )
        )
    ).scalar_one_or_none()
    if binding is None or not binding.enabled:
        raise MCPKillSwitchError("专家绑定已关闭，调用被阻断")
    if tool.sensitive:
        if tool.authorized_at is None:
            raise MCPKillSwitchError("敏感工具授权已撤销，调用被阻断")
        snapshot_authorized = _parse_snapshot_dt(entry.get("authorized_at"))
        db_authorized = tool.authorized_at
        if db_authorized.tzinfo is not None:
            db_authorized = db_authorized.astimezone(timezone.utc).replace(tzinfo=None)
        if snapshot_authorized is not None and db_authorized < snapshot_authorized:
            raise MCPKillSwitchError("敏感工具授权早于快照时点，调用被阻断")


async def execute_task_tool_call(
    db: AsyncSession,
    settings: Settings,
    *,
    server_id: int,
    tool_name: str,
    args: dict,
    client_factory=None,
) -> dict:
    """连接 Server 执行 tools/call；传输/协议失败 → 502。"""
    server = await db.get(MCPServer, server_id)
    if server is None:
        raise MCPServerNotFoundError("MCP Server 不存在")
    factory = client_factory or _default_client_factory(settings)
    client = factory(server)
    try:
        return await client.call(tool_name, args)
    except MCPClientError as exc:
        logger.warning("tools/call server=%s tool=%s 失败: %s", server_id, tool_name, exc)
        raise MCPUpstreamError("MCP Server 调用失败，请稍后重试") from exc
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# 任务快照装配（§12 决策 15：能力上限）
# ---------------------------------------------------------------------------


async def load_snapshot_tools(db: AsyncSession, expert_id: int) -> list[dict]:
    """创建任务时冻结的 MCP 工具能力上限。

    enabled 绑定 ∩ published Server ∩ enabled 工具 ∩（非敏感 或 已授权）；
    条目含 authorized_at（运行期 kill switch 校验「授权不早于快照时点」用）。
    """
    rows = (
        await db.execute(
            select(MCPTool)
            .join(MCPServer, MCPServer.id == MCPTool.server_id)
            .join(ExpertMCP, ExpertMCP.server_id == MCPServer.id)
            .where(
                ExpertMCP.expert_id == expert_id,
                ExpertMCP.enabled.is_(True),
                MCPServer.status == "published",
                MCPTool.enabled.is_(True),
                or_(MCPTool.sensitive.is_(False), MCPTool.authorized_at.is_not(None)),
            )
            .order_by(ExpertMCP.id, MCPTool.id)
        )
    ).scalars()
    return [
        {
            "name": tool.name,
            "label": tool.name,
            "description": tool.description,
            "schema": json.loads(tool.input_schema) if tool.input_schema else {"type": "object"},
            "serverId": tool.server_id,
            "sensitive": bool(tool.sensitive),
            "authorized_at": tool.authorized_at.isoformat() if tool.authorized_at else None,
        }
        for tool in rows
    ]
