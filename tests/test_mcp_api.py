"""MCP 管理 API 契约测试（手册 §6.7 / §6.3）。

- /api/mcp/servers 全套：创建（transport 条件校验、env 只回变量名）、列表分页、
  详情含工具、部分更新、discover（fake 客户端注入）、工具开关（敏感确认）、
  publish/offline 状态机、删除引用阻断
- /api/experts/{id}/mcp 三端点：绑定/更新/解绑；归属与状态校验
- 专家详情 mcps 字段随绑定填充（§6.3：连接信息不返回）
"""

import base64
import os

import pytest

from backend.config import Settings, get_settings
from backend.main import app
from backend.services import mcp_service
from tests.test_experts import create_expert
from tests.test_skills import auth_header, register_expert

pytestmark = pytest.mark.usefixtures("client")


class FakeMcpClient:
    def __init__(self, tools) -> None:
        self._tools = tools

    async def discover(self):
        return self._tools

    async def close(self):
        return None


@pytest.fixture()
def crypto_settings():
    raw = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    settings = Settings(
        MCP_ENCRYPTION_ACTIVE_KID="primary",
        MCP_ENCRYPTION_KEYRING=f"primary:{raw}",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    yield settings
    app.dependency_overrides.pop(get_settings, None)


def create_server(client, token, **overrides):
    payload = {
        "name": "文件系统",
        "description": "访问授权目录内文件的 MCP Server",
        "transport": "stdio",
        "command": "mcp-server-filesystem /workspace",
        "env_vars": {"FS_TOKEN": "secret-value-1"},
        **overrides,
    }
    return client.post("/api/mcp/servers", json=payload, headers=auth_header(token))


def tool_entry(name, schema=None):
    return {
        "name": name,
        "description": f"{name} 工具",
        "input_schema": schema or {"type": "object", "properties": {}},
    }


# ---------------------------------------------------------------------------
# Server CRUD
# ---------------------------------------------------------------------------


def test_create_server_hides_env_and_defaults_draft(client, crypto_settings):
    token, _ = register_expert(client)
    response = create_server(client, token)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "draft"
    assert data["env_var_names"] == ["FS_TOKEN"]
    assert "env_vars" not in data
    assert "secret" not in response.text


def test_create_stdio_requires_command(client, crypto_settings):
    token, _ = register_expert(client)
    response = create_server(client, token, command=None)
    assert response.status_code == 400


def test_create_http_requires_url(client, crypto_settings):
    token, _ = register_expert(client)
    response = create_server(client, token, transport="http-sse", command=None, url=None)
    assert response.status_code == 400


def test_list_servers_paginated(client, crypto_settings):
    token, _ = register_expert(client)
    create_server(client, token, name="第一")
    create_server(client, token, name="第二")
    response = client.get("/api/mcp/servers?page=1&size=1", headers=auth_header(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["data"]) == 1


def test_server_detail_scoped_to_owner(client, crypto_settings):
    token, _ = register_expert(client)
    other_token, _ = register_expert(client, username="other", email="other@example.com")
    server_id = create_server(client, token).json()["data"]["id"]
    ok = client.get(f"/api/mcp/servers/{server_id}", headers=auth_header(token))
    assert ok.status_code == 200
    foreign = client.get(f"/api/mcp/servers/{server_id}", headers=auth_header(other_token))
    assert foreign.status_code == 404


def test_update_server_partial(client, crypto_settings):
    token, _ = register_expert(client)
    server_id = create_server(client, token).json()["data"]["id"]
    response = client.put(
        f"/api/mcp/servers/{server_id}",
        json={"description": "更新后的描述"},
        headers=auth_header(token),
    )
    assert response.status_code == 200
    assert response.json()["data"]["description"] == "更新后的描述"
    assert response.json()["data"]["command"] == "mcp-server-filesystem /workspace"


# ---------------------------------------------------------------------------
# discover / 工具开关
# ---------------------------------------------------------------------------


def _stub_discover_tools(monkeypatch, tools):
    def factory(settings):
        def build(server):
            return FakeMcpClient(tools)

        return build

    monkeypatch.setattr(mcp_service, "_default_client_factory", factory)


def test_discover_persists_and_returns_tools(client, crypto_settings, monkeypatch):
    token, _ = register_expert(client)
    server_id = create_server(client, token).json()["data"]["id"]
    _stub_discover_tools(monkeypatch, [tool_entry("list_directory"), tool_entry("write_file")])
    response = client.post(f"/api/mcp/servers/{server_id}/discover", headers=auth_header(token))
    assert response.status_code == 200
    tools = response.json()["data"]["tools"]
    by_name = {t["name"]: t for t in tools}
    assert by_name["list_directory"]["sensitive"] is False  # 可信只读 allowlist
    assert by_name["write_file"]["sensitive"] is True
    assert by_name["write_file"]["enabled"] is False
    # 详情可见
    detail = client.get(f"/api/mcp/servers/{server_id}", headers=auth_header(token))
    assert len(detail.json()["data"]["tools"]) == 2


def test_discover_upstream_failure_502(client, crypto_settings, monkeypatch):
    from backend.engine.mcp_client import MCPClientError

    class FailingClient:
        async def discover(self):
            raise MCPClientError("refused")

        async def close(self):
            return None

    def factory(settings):
        return lambda server: FailingClient()

    monkeypatch.setattr(mcp_service, "_default_client_factory", factory)
    token, _ = register_expert(client)
    server_id = create_server(client, token).json()["data"]["id"]
    response = client.post(f"/api/mcp/servers/{server_id}/discover", headers=auth_header(token))
    assert response.status_code == 502


def test_tool_enable_sensitive_records_authorization(client, crypto_settings, monkeypatch):
    token, _ = register_expert(client)
    server_id = create_server(client, token).json()["data"]["id"]
    _stub_discover_tools(monkeypatch, [tool_entry("write_file")])
    client.post(f"/api/mcp/servers/{server_id}/discover", headers=auth_header(token))
    detail = client.get(f"/api/mcp/servers/{server_id}", headers=auth_header(token))
    tool_id = detail.json()["data"]["tools"][0]["id"]
    enabled = client.put(
        f"/api/mcp/servers/{server_id}/tools/{tool_id}",
        json={"enabled": True},
        headers=auth_header(token),
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["authorized_at"] is not None
    disabled = client.put(
        f"/api/mcp/servers/{server_id}/tools/{tool_id}",
        json={"enabled": False},
        headers=auth_header(token),
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["authorized_at"] is None


# ---------------------------------------------------------------------------
# publish / offline / delete
# ---------------------------------------------------------------------------


def test_publish_requires_discovered_tools(client, crypto_settings, monkeypatch):
    token, _ = register_expert(client)
    server_id = create_server(client, token).json()["data"]["id"]
    empty = client.post(f"/api/mcp/servers/{server_id}/publish", headers=auth_header(token))
    assert empty.status_code == 400
    _stub_discover_tools(monkeypatch, [tool_entry("list_directory")])
    client.post(f"/api/mcp/servers/{server_id}/discover", headers=auth_header(token))
    published = client.post(f"/api/mcp/servers/{server_id}/publish", headers=auth_header(token))
    assert published.status_code == 200
    assert published.json()["data"]["status"] == "published"


def test_offline_state_machine(client, crypto_settings, monkeypatch):
    token, _ = register_expert(client)
    server_id = create_server(client, token).json()["data"]["id"]
    draft = client.post(f"/api/mcp/servers/{server_id}/offline", headers=auth_header(token))
    assert draft.status_code == 400
    _stub_discover_tools(monkeypatch, [tool_entry("list_directory")])
    client.post(f"/api/mcp/servers/{server_id}/discover", headers=auth_header(token))
    client.post(f"/api/mcp/servers/{server_id}/publish", headers=auth_header(token))
    offline = client.post(f"/api/mcp/servers/{server_id}/offline", headers=auth_header(token))
    assert offline.status_code == 200
    assert offline.json()["data"]["status"] == "offline"


def test_delete_blocked_by_binding_then_ok(client, crypto_settings, monkeypatch):
    token, _ = register_expert(client)
    server_id = create_server(client, token).json()["data"]["id"]
    _stub_discover_tools(monkeypatch, [tool_entry("list_directory")])
    client.post(f"/api/mcp/servers/{server_id}/discover", headers=auth_header(token))
    client.post(f"/api/mcp/servers/{server_id}/publish", headers=auth_header(token))
    expert = create_expert(client, token).json()["data"]
    client.post(
        f"/api/experts/{expert['id']}/mcp",
        json={"server_id": server_id, "enabled": False},
        headers=auth_header(token),
    )
    blocked = client.delete(f"/api/mcp/servers/{server_id}", headers=auth_header(token))
    assert blocked.status_code == 409
    client.delete(f"/api/experts/{expert['id']}/mcp/{server_id}", headers=auth_header(token))
    deleted = client.delete(f"/api/mcp/servers/{server_id}", headers=auth_header(token))
    assert deleted.status_code == 200
    assert deleted.json()["data"]["message"] == "deleted"


# ---------------------------------------------------------------------------
# 专家绑定三端点 + 专家详情
# ---------------------------------------------------------------------------


def test_expert_mcp_bind_update_unbind(client, crypto_settings, monkeypatch):
    token, _ = register_expert(client)
    server_id = create_server(client, token).json()["data"]["id"]
    _stub_discover_tools(monkeypatch, [tool_entry("list_directory")])
    client.post(f"/api/mcp/servers/{server_id}/discover", headers=auth_header(token))
    client.post(f"/api/mcp/servers/{server_id}/publish", headers=auth_header(token))
    expert = create_expert(client, token).json()["data"]

    bound = client.post(
        f"/api/experts/{expert['id']}/mcp",
        json={"server_id": server_id},
        headers=auth_header(token),
    )
    assert bound.status_code == 201
    assert bound.json()["data"] == {
        "expert_id": expert["id"],
        "server_id": server_id,
        "enabled": False,
    }

    enabled = client.put(
        f"/api/experts/{expert['id']}/mcp/{server_id}",
        json={"enabled": True},
        headers=auth_header(token),
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["enabled"] is True

    # 专家详情 mcps 随绑定填充
    detail = client.get(f"/api/experts/{expert['id']}", headers=auth_header(token))
    assert detail.json()["data"]["mcps"] == [
        {"id": server_id, "name": "文件系统", "status": "published", "enabled": True}
    ]

    unbound = client.delete(
        f"/api/experts/{expert['id']}/mcp/{server_id}", headers=auth_header(token)
    )
    assert unbound.status_code == 200
    assert unbound.json()["data"]["message"] == "unbound"


def test_bind_draft_server_400_and_foreign_404(client, crypto_settings, monkeypatch):
    token, _ = register_expert(client)
    other_token, _ = register_expert(client, username="other", email="other@example.com")
    expert = create_expert(client, token).json()["data"]
    draft_id = create_server(client, token).json()["data"]["id"]  # 不 discover/publish
    foreign_id = create_server(client, other_token, name="他人服务").json()["data"]["id"]

    draft = client.post(
        f"/api/experts/{expert['id']}/mcp",
        json={"server_id": draft_id},
        headers=auth_header(token),
    )
    assert draft.status_code == 400
    foreign = client.post(
        f"/api/experts/{expert['id']}/mcp",
        json={"server_id": foreign_id},
        headers=auth_header(token),
    )
    assert foreign.status_code == 404


def test_unbind_missing_404(client, crypto_settings):
    token, _ = register_expert(client)
    expert = create_expert(client, token).json()["data"]
    response = client.delete(f"/api/experts/{expert['id']}/mcp/999", headers=auth_header(token))
    assert response.status_code == 404
