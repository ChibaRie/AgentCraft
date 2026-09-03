"""MCP 管理服务层测试（手册 §6.7/§11.3、DB 设计 §3.9-§3.11）。

- 创建：transport 条件必填（stdio→command、http-sse→url）；env_vars 落库为
  AES-256-GCM 信封（AAD 绑定 server_id），响应只含变量名
- discover：fake 客户端 tools/list → 工具落库（默认敏感+关闭）；可信只读
  allowlist 置非敏感；重复 discover 更新描述/schema 且保留启用状态
- 工具开关：敏感工具启用需确认（否则 400），disable 清空 authorized_at
- publish 需 ≥1 已发现工具；offline 仅限 published
- 删除：仍有绑定或任务快照引用 → 409
- 专家绑定：归属一致、Server 必须 published、重复绑定 409
- 快照装配 load_snapshot_tools：enabled 绑定 ∩ published Server ∩
  enabled 工具 ∩ 敏感已授权
"""

import base64
import json
import os

import pytest
from sqlalchemy import select

from backend.config import Settings
from backend.models.expert import Expert
from backend.models.mcp_server import MCPServer
from backend.models.task import Task
from backend.models.user import User
from backend.services import mcp_service
from backend.services.mcp_service import (
    MCPServerInvalidError,
    MCPServerNotFoundError,
)
from backend.utils.crypto import decrypt_text, make_keyring, mcp_env_aad

KEY = os.urandom(32)
KID = "primary"


def make_settings(**overrides) -> Settings:
    raw = base64.urlsafe_b64encode(KEY).decode().rstrip("=")
    return Settings(
        MCP_ENCRYPTION_ACTIVE_KID=KID,
        MCP_ENCRYPTION_KEYRING=f"{KID}:{raw}",
        HOST_DATA_ROOT="./data",
        HOST_WORKSPACE_ROOT="./workspaces",
        **overrides,
    )


async def seed_user(db_factory, username: str = "mcp-owner") -> int:
    async with db_factory() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="x",
            role="expert",
        )
        session.add(user)
        await session.commit()
        return user.id


async def seed_expert(db_factory, owner_id: int, name: str = "绑专家") -> int:
    async with db_factory() as session:
        expert = Expert(
            owner_id=owner_id,
            name=name,
            description="MCP 测试专家",
            category="tech",
            persona="p" * 10,
            methodology="m" * 10,
        )
        session.add(expert)
        await session.commit()
        return expert.id


async def seed_published_server(db_factory, owner_id: int, with_tools: bool = True) -> int:
    async with db_factory() as session:
        server = MCPServer(
            owner_id=owner_id,
            name="已发布服务",
            description="种子 MCP Server",
            transport="http-sse",
            url="http://mcp-sandbox:3000/mcp",
            status="published",
        )
        session.add(server)
        await session.flush()
        if with_tools:
            from backend.models.mcp_tool import MCPTool

            session.add(
                MCPTool(
                    server_id=server.id,
                    name="list_directory",
                    description="列出目录",
                    input_schema='{"type":"object"}',
                    sensitive=False,
                    enabled=True,
                )
            )
        await session.commit()
        return server.id


# ---------------------------------------------------------------------------
# 创建与校验
# ---------------------------------------------------------------------------


async def test_create_stdio_requires_command(test_db):
    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    with pytest.raises(MCPServerInvalidError):
        await mcp_service.create_server(
            db, owner,
            name="fs", description="文件系统", transport="stdio",
            command=None, url=None, env_vars=None,
            settings=make_settings(),
        )


async def test_create_http_requires_url(test_db):
    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    with pytest.raises(MCPServerInvalidError):
        await mcp_service.create_server(
            db, owner,
            name="fs", description="文件系统", transport="http-sse",
            command="mcp-server-fs", url=None, env_vars=None,
            settings=make_settings(),
        )


async def test_create_rejects_unknown_transport(test_db):
    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    with pytest.raises(MCPServerInvalidError):
        await mcp_service.create_server(
            db, owner,
            name="fs", description="x", transport="websocket",
            command="x", url=None, env_vars=None,
            settings=make_settings(),
        )


async def test_create_encrypts_env_vars_and_hides_from_payload(test_db):
    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    server = await mcp_service.create_server(
        db, owner,
        name="fs", description="文件系统", transport="stdio",
        command="mcp-server-fs /workspace",
        env_vars={"FS_TOKEN": "secret-value-1"},
        settings=make_settings(),
    )
    assert server.env_vars  # 信封已落库
    envelope = json.loads(server.env_vars)
    _, keyring = make_keyring(make_settings().MCP_ENCRYPTION_KEYRING, KID)
    plaintext = decrypt_text(
        envelope, aad=mcp_env_aad(server.id), keyring=keyring
    )
    assert json.loads(plaintext) == {"FS_TOKEN": "secret-value-1"}
    payload = mcp_service.server_payload(server, settings=make_settings())
    assert "env_vars" not in payload  # 信封/明文不出 API
    assert payload["env_var_names"] == ["FS_TOKEN"]
    assert payload["status"] == "draft"


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


class FakeMcpClient:
    def __init__(self, tools=None, error: Exception | None = None) -> None:
        self._tools = tools or []
        self._error = error

    async def discover(self):
        if self._error:
            raise self._error
        return self._tools

    async def close(self):
        return None


def tool_entry(name, description="工具", schema=None):
    return {"name": name, "description": description, "input_schema": schema or {"type": "object"}}


async def test_discover_persists_tools_conservative(test_db):
    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    server = await mcp_service.create_server(
        db, owner,
        name="fs", description="文件系统", transport="stdio",
        command="mcp-server-fs /workspace", env_vars=None, settings=make_settings(),
    )
    tools = await mcp_service.discover_tools(
        db, owner, server.id, make_settings(),
        client_factory=lambda server: FakeMcpClient(
            [tool_entry("list_directory"), tool_entry("send_email")]
        ),
    )
    by_name = {t["name"]: t for t in tools}
    # 可信只读 allowlist → 非敏感；其余默认敏感
    assert by_name["list_directory"]["sensitive"] is False
    assert by_name["send_email"]["sensitive"] is True
    assert by_name["send_email"]["enabled"] is False  # 新发现工具默认关闭


async def test_discover_refreshes_but_preserves_state(test_db):
    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    server = await mcp_service.create_server(
        db, owner, name="fs", description="f", transport="http-sse",
        url="http://mcp/mcp", env_vars=None, settings=make_settings(),
    )
    await mcp_service.discover_tools(
        db, owner, server.id, make_settings(),
        client_factory=lambda s: FakeMcpClient([tool_entry("list_directory")]),
    )
    # 手动启用后再次 discover：状态保留，schema 更新
    tool = await mcp_service.update_tool(db, owner, server.id, 1, enabled=True)
    assert tool.enabled is True
    tools = await mcp_service.discover_tools(
        db, owner, server.id, make_settings(),
        client_factory=lambda s: FakeMcpClient(
            [tool_entry("list_directory", schema={"type": "object", "properties": {}})]
        ),
    )
    assert tools[0]["enabled"] is True
    assert tools[0]["input_schema"] == {"type": "object", "properties": {}}


async def test_discover_connection_failure_maps_502(test_db):
    from backend.engine.mcp_client import MCPClientError

    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    server = await mcp_service.create_server(
        db, owner, name="fs", description="f", transport="http-sse",
        url="http://mcp/mcp", env_vars=None, settings=make_settings(),
    )
    with pytest.raises(mcp_service.MCPUpstreamError) as excinfo:
        await mcp_service.discover_tools(
            db, owner, server.id, make_settings(),
            client_factory=lambda s: FakeMcpClient(error=MCPClientError("refused")),
        )
    assert excinfo.value.status_code == 502


async def test_discover_owned_check(test_db):
    owner = await seed_user(test_db.session_factory)
    other = await seed_user(test_db.session_factory, "other")
    server_id = await seed_published_server(test_db.session_factory, owner)
    db = test_db.session_factory()
    with pytest.raises(MCPServerNotFoundError):
        await mcp_service.discover_tools(
            db, other, server_id, make_settings(), client_factory=lambda s: FakeMcpClient([])
        )


# ---------------------------------------------------------------------------
# 工具开关 / publish / offline / delete
# ---------------------------------------------------------------------------


async def _server_with_tool(
    db_factory, owner_id, *, sensitive, enabled=False, status="draft"
):
    """独立创建 Server + 单工具（seed_published_server 恒为已发布+已启用工具）。"""
    from backend.models.mcp_tool import MCPTool

    async with db_factory() as session:
        server = MCPServer(
            owner_id=owner_id,
            name="工具服务",
            description="d",
            transport="http-sse",
            url="http://mcp/mcp",
            status=status,
        )
        session.add(server)
        await session.flush()
        session.add(
            MCPTool(
                server_id=server.id,
                name="write_file",
                description="写文件",
                input_schema='{"type":"object"}',
                sensitive=sensitive,
                enabled=enabled,
            )
        )
        await session.commit()
        return server.id


async def test_sensitive_tool_enable_records_authorization(test_db):
    """敏感工具直接开关：启用即记录授权时点，禁用清空（用户要求免多次提醒）。"""
    owner = await seed_user(test_db.session_factory)
    server_id = await _server_with_tool(test_db.session_factory, owner, sensitive=True)
    db = test_db.session_factory()
    tool = await mcp_service.update_tool(db, owner, server_id, 1, enabled=True)
    assert tool.enabled is True
    assert tool.authorized_at is not None
    # 禁用清空授权
    tool = await mcp_service.update_tool(db, owner, server_id, 1, enabled=False)
    assert tool.authorized_at is None


async def test_non_sensitive_tool_enables_without_confirm(test_db):
    owner = await seed_user(test_db.session_factory)
    server_id = await _server_with_tool(test_db.session_factory, owner, sensitive=False)
    db = test_db.session_factory()
    tool = await mcp_service.update_tool(db, owner, server_id, 1, enabled=True)
    assert tool.enabled is True and tool.authorized_at is None


async def test_publish_requires_discovered_tools(test_db):
    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    server = await mcp_service.create_server(
        db, owner, name="fs", description="f", transport="http-sse",
        url="http://mcp/mcp", env_vars=None, settings=make_settings(),
    )
    with pytest.raises(MCPServerInvalidError):
        await mcp_service.publish_server(db, owner, server.id)
    server_id = await _server_with_tool(test_db.session_factory, owner, sensitive=False)
    published = await mcp_service.publish_server(db, owner, server_id)
    assert published.status == "published"


async def test_offline_only_from_published(test_db):
    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    server = await mcp_service.create_server(
        db, owner, name="fs", description="f", transport="http-sse",
        url="http://mcp/mcp", env_vars=None, settings=make_settings(),
    )
    with pytest.raises(MCPServerInvalidError):
        await mcp_service.offline_server(db, owner, server.id)
    server_id = await _server_with_tool(
        test_db.session_factory, owner, sensitive=False, status="published"
    )
    offline = await mcp_service.offline_server(db, owner, server_id)
    assert offline.status == "offline"


async def test_delete_blocked_by_binding_or_snapshot(test_db):
    owner = await seed_user(test_db.session_factory)
    expert_id = await seed_expert(test_db.session_factory, owner)
    server_id = await seed_published_server(test_db.session_factory, owner)
    db = test_db.session_factory()
    # 快照引用（另一任务）→ 409
    async with test_db.session_factory() as session:
        session.add(
            Task(
                user_id=owner, expert_id=expert_id, expert_name_snapshot="e", title="t",
                status="running", skill_snapshot="{}",
                mcp_snapshot=json.dumps({"tools": [{"serverId": server_id}]}),
                provider_snapshot="{}", workdir="/workspaces/authorized",
            )
        )
        await session.commit()
    with pytest.raises(mcp_service.MCPServerReferencedError) as excinfo:
        await mcp_service.delete_server(db, owner, server_id)
    assert excinfo.value.status_code == 409
    # 解除快照引用但仍有绑定 → 409
    async with test_db.session_factory() as session:
        from backend.models.expert_mcp import ExpertMCP

        session.add(ExpertMCP(expert_id=expert_id, server_id=server_id, enabled=False))
        await session.commit()
    async with test_db.session_factory() as session:
        task = (await session.execute(select(Task))).scalars().first()
        task.mcp_snapshot = json.dumps({"tools": []})
        await session.commit()
    with pytest.raises(mcp_service.MCPServerReferencedError):
        await mcp_service.delete_server(db, owner, server_id)
    # 解绑后可删除
    await mcp_service.unbind_server(db, owner, expert_id, server_id)
    await mcp_service.delete_server(db, owner, server_id)


# ---------------------------------------------------------------------------
# 专家绑定
# ---------------------------------------------------------------------------


async def test_bind_requires_published_and_same_owner(test_db):
    owner = await seed_user(test_db.session_factory)
    other = await seed_user(test_db.session_factory, "other")
    expert_id = await seed_expert(test_db.session_factory, owner)
    foreign_server = await seed_published_server(test_db.session_factory, other)
    db = test_db.session_factory()
    own_server = await seed_published_server(test_db.session_factory, owner)
    draft_server = await mcp_service.create_server(
        db, owner,
        name="草稿", description="d", transport="http-sse",
        url="http://mcp/mcp", env_vars=None, settings=make_settings(),
    )
    # 他人 Server：统一 404（不暴露存在性）
    with pytest.raises(MCPServerNotFoundError):
        await mcp_service.bind_server(db, owner, expert_id, foreign_server, enabled=False)
    # 未发布 Server：400
    with pytest.raises(MCPServerInvalidError):
        await mcp_service.bind_server(db, owner, expert_id, draft_server.id, enabled=False)
    # 正常绑定，默认 disabled
    binding = await mcp_service.bind_server(db, owner, expert_id, own_server, enabled=False)
    assert binding.enabled is False
    # 重复绑定 409
    with pytest.raises(mcp_service.MCPAlreadyBoundError):
        await mcp_service.bind_server(db, owner, expert_id, own_server, enabled=False)


async def test_binding_update_and_unbind_404(test_db):
    owner = await seed_user(test_db.session_factory)
    expert_id = await seed_expert(test_db.session_factory, owner)
    server_id = await seed_published_server(test_db.session_factory, owner)
    db = test_db.session_factory()
    with pytest.raises(mcp_service.MCPBindingNotFoundError):
        await mcp_service.update_binding(db, owner, expert_id, server_id, enabled=True)
    await mcp_service.bind_server(db, owner, expert_id, server_id, enabled=False)
    binding = await mcp_service.update_binding(db, owner, expert_id, server_id, enabled=True)
    assert binding.enabled is True
    await mcp_service.unbind_server(db, owner, expert_id, server_id)
    with pytest.raises(mcp_service.MCPBindingNotFoundError):
        await mcp_service.unbind_server(db, owner, expert_id, server_id)


# ---------------------------------------------------------------------------
# 快照装配
# ---------------------------------------------------------------------------


async def test_load_snapshot_tools_filters_and_shapes(test_db):
    owner = await seed_user(test_db.session_factory)
    expert_id = await seed_expert(test_db.session_factory, owner)
    server_id = await seed_published_server(test_db.session_factory, owner)
    db = test_db.session_factory()
    # 无绑定 → 空集
    assert await mcp_service.load_snapshot_tools(db, expert_id) == []
    await mcp_service.bind_server(db, owner, expert_id, server_id, enabled=True)
    tools = await mcp_service.load_snapshot_tools(db, expert_id)
    assert tools == [
        {
            "name": "list_directory",
            "label": "list_directory",
            "description": "列出目录",
            "schema": {"type": "object"},
            "serverId": server_id,
            "sensitive": False,
            "authorized_at": None,
        }
    ]


async def test_load_snapshot_excludes_disabled_tool_and_binding(test_db):
    """工具禁用或绑定关闭均不进入快照（敏感+enabled 无授权的状态被 DB CHECK
    约束排除，快照查询的授权过滤是纵深防御）。"""
    from backend.models.expert_mcp import ExpertMCP
    from backend.models.mcp_tool import MCPTool

    owner = await seed_user(test_db.session_factory)
    expert_id = await seed_expert(test_db.session_factory, owner)
    server_id = await seed_published_server(test_db.session_factory, owner, with_tools=False)
    async with test_db.session_factory() as session:
        session.add(
            MCPTool(
                server_id=server_id, name="write_file", description="写",
                input_schema='{"type":"object"}', sensitive=True, enabled=False,
                authorized_at=None,
            )
        )
        session.add(ExpertMCP(expert_id=expert_id, server_id=server_id, enabled=True))
        await session.commit()
    db = test_db.session_factory()
    assert await mcp_service.load_snapshot_tools(db, expert_id) == []


# ---------------------------------------------------------------------------
# 运行期 env 解密（/internal/mcp/call 用）
# ---------------------------------------------------------------------------


async def test_runtime_env_roundtrip_and_server_client_build(test_db):
    owner = await seed_user(test_db.session_factory)
    db = test_db.session_factory()
    server = await mcp_service.create_server(
        db, owner,
        name="fs", description="f", transport="stdio",
        command="mcp-server-fs /workspace",
        env_vars={"FS_TOKEN": "secret-value-1"},
        settings=make_settings(),
    )
    env = mcp_service.decrypt_server_env(server, make_settings())
    assert env == {"FS_TOKEN": "secret-value-1"}
    # http Server：env 加密缺席时为空 dict
    http_server = await mcp_service.create_server(
        db, owner, name="h", description="f", transport="http-sse",
        url="http://mcp/mcp", env_vars=None, settings=make_settings(),
    )
    assert mcp_service.decrypt_server_env(http_server, make_settings()) == {}
