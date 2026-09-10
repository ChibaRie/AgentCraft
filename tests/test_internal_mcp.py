"""/internal/mcp/call 测试（手册 §6.8：任务令牌鉴权 + 快照/kill switch 双层校验）。

- 鉴权：X-Task-Token 缺失/非法 401；body task_id 与令牌不符 401；
  令牌与当前容器实例不符（manager 无此令牌）401
- 能力上限：工具不在 tasks.mcp_snapshot → 404
- kill switch（快照可见 ≠ 可调用）：Server 非 published / 工具禁用 /
  绑定禁用 / 敏感授权缺失或早于快照时点 → 403
- 执行：tools/call 成功 200 {content, is_error:false}；工具自身失败
  is_error:true 透传；上游失败 502
"""

import base64
import json
import os
from datetime import datetime

import pytest

from backend.config import Settings, get_settings
from backend.dependencies import get_pi_engine_manager
from backend.main import app
from backend.models.expert import Expert
from backend.models.expert_mcp import ExpertMCP
from backend.models.mcp_server import MCPServer
from backend.models.mcp_tool import MCPTool
from backend.models.task import Task
from backend.models.user import User
from backend.services.task_token import create_task_token

pytestmark = pytest.mark.usefixtures("client")

KEY = os.urandom(32)
KID = "primary"


class FakeManager:
    def __init__(self) -> None:
        self.tokens: dict[int, str] = {}

    def get_task_token(self, task_id: int):
        return self.tokens.get(task_id)


@pytest.fixture()
def mcp_env(test_db, tmp_path):
    raw = base64.urlsafe_b64encode(KEY).decode().rstrip("=")
    settings = Settings(
        MCP_ENCRYPTION_ACTIVE_KID=KID,
        MCP_ENCRYPTION_KEYRING=f"{KID}:{raw}",
        HOST_DATA_ROOT=str(tmp_path / "data"),
        HOST_WORKSPACE_ROOT=str(tmp_path / "workspaces"),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    manager = FakeManager()
    app.dependency_overrides[get_pi_engine_manager] = lambda: manager
    yield SimpleEnv(test_db, manager, settings)
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_pi_engine_manager, None)


class SimpleEnv:
    def __init__(self, test_db, manager, settings) -> None:
        self.db = test_db
        self.manager = manager
        self.settings = settings


async def seed_task(
    db_env,
    *,
    tool_sensitive: bool = False,
    tool_enabled: bool = True,
    tool_authorized: datetime | None = None,
    binding_enabled: bool = True,
    server_status: str = "published",
    snapshot_entry: dict | None = None,
) -> int:
    """播种 user/expert/server/tool/binding/task；返回 task_id。"""
    factory = db_env.db.session_factory
    async with factory() as session:
        user = User(
            username="mcp-task-u", email="mcp-task-u@example.com", password_hash="x", role="expert"
        )
        session.add(user)
        await session.flush()
        expert = Expert(
            owner_id=user.id,
            name="MCP专家",
            description="d",
            category="tech",
            persona="p" * 10,
            methodology="m" * 10,
        )
        session.add(expert)
        await session.flush()
        server = MCPServer(
            owner_id=user.id,
            name="fs",
            description="f",
            transport="http-sse",
            url="http://mcp/mcp",
            status=server_status,
        )
        session.add(server)
        await session.flush()
        session.add(
            MCPTool(
                server_id=server.id,
                name="list_directory",
                description="列目录",
                input_schema='{"type":"object"}',
                sensitive=tool_sensitive,
                enabled=tool_enabled,
                authorized_at=tool_authorized,
            )
        )
        session.add(ExpertMCP(expert_id=expert.id, server_id=server.id, enabled=binding_enabled))
        entry = snapshot_entry or {
            "name": "list_directory",
            "label": "list_directory",
            "description": "列目录",
            "schema": {"type": "object"},
            "serverId": server.id,
            "sensitive": tool_sensitive,
            "authorized_at": tool_authorized.isoformat() if tool_authorized else None,
        }
        if entry.get("serverId") == "SELF":  # 测试哨兵：引用本任务自己的 server
            entry["serverId"] = server.id
        task = Task(
            user_id=user.id,
            expert_id=expert.id,
            expert_name_snapshot="MCP专家",
            title="t",
            status="running",
            skill_snapshot="{}",
            mcp_snapshot=json.dumps({"tools": [entry]}),
            provider_snapshot=json.dumps(
                {
                    "source": "system",
                    "protocol": "openai",
                    "base_url": "http://proxy:8080/v1",
                    "model_id": "m",
                }
            ),
            workdir="/workspaces/authorized",
        )
        session.add(task)
        await session.commit()
        return task.id, server.id, user.id


def call(client, token, task_id, server_id, tool_name="list_directory", args=None):
    return client.post(
        "/internal/mcp/call",
        json={
            "task_id": task_id,
            "server_id": server_id,
            "tool_name": tool_name,
            "args": args or {},
        },
        headers={"X-Task-Token": token},
    )


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------


def test_missing_token_401(client, mcp_env):
    response = client.post(
        "/internal/mcp/call",
        json={"task_id": 1, "server_id": 1, "tool_name": "x", "args": {}},
    )
    assert response.status_code == 401


def test_bad_token_401(client, mcp_env):
    response = call(client, "garbage", 1, 1)
    assert response.status_code == 401


def test_token_task_mismatch_401(client, mcp_env):
    task_id, server_id, _user_id = mcp_env.db.run(seed_task(db_env=mcp_env))
    other_token = create_task_token(task_id + 100, instance="inst-a", model_id="m")
    mcp_env.manager.tokens[task_id] = create_task_token(task_id, instance="inst-a", model_id="m")
    response = call(client, other_token, task_id, server_id)
    assert response.status_code == 401


def test_stale_instance_token_401(client, mcp_env):
    """容器已重建：manager 只认当前实例令牌。"""
    task_id, server_id, _ = mcp_env.db.run(seed_task(db_env=mcp_env))
    stale = create_task_token(task_id, instance="old-instance", model_id="m")
    mcp_env.manager.tokens[task_id] = create_task_token(
        task_id, instance="new-instance", model_id="m"
    )
    response = call(client, stale, task_id, server_id)
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 能力上限 + kill switch
# ---------------------------------------------------------------------------


def test_tool_not_in_snapshot_404(client, mcp_env):
    task_id, server_id, _ = mcp_env.db.run(seed_task(db_env=mcp_env))
    mcp_env.manager.tokens[task_id] = create_task_token(task_id, instance="i", model_id="m")
    response = call(
        client, mcp_env.manager.tokens[task_id], task_id, server_id, tool_name="not_registered"
    )
    assert response.status_code == 404


def test_server_offline_blocks_403(client, mcp_env):
    task_id, server_id, _ = mcp_env.db.run(seed_task(db_env=mcp_env, server_status="offline"))
    token = create_task_token(task_id, instance="i", model_id="m")
    mcp_env.manager.tokens[task_id] = token
    response = call(client, token, task_id, server_id)
    assert response.status_code == 403


def test_tool_disabled_blocks_403(client, mcp_env):
    task_id, server_id, _ = mcp_env.db.run(seed_task(db_env=mcp_env, tool_enabled=False))
    token = create_task_token(task_id, instance="i", model_id="m")
    mcp_env.manager.tokens[task_id] = token
    response = call(client, token, task_id, server_id)
    assert response.status_code == 403


def test_binding_disabled_blocks_403(client, mcp_env):
    task_id, server_id, _ = mcp_env.db.run(seed_task(db_env=mcp_env, binding_enabled=False))
    token = create_task_token(task_id, instance="i", model_id="m")
    mcp_env.manager.tokens[task_id] = token
    response = call(client, token, task_id, server_id)
    assert response.status_code == 403


def test_sensitive_tool_stale_authorization_blocks_403(client, mcp_env):
    """DB 授权早于快照授权时点 → 403（§6.8：不早于快照授权时点）。"""
    task_id, server_id, _ = mcp_env.db.run(
        seed_task(
            db_env=mcp_env,
            tool_sensitive=True,
            tool_authorized=datetime(2026, 9, 1, 12, 0, 0),
            snapshot_entry={
                "name": "list_directory",
                "label": "list_directory",
                "description": "列目录",
                "schema": {"type": "object"},
                "serverId": "SELF",
                "sensitive": True,
                "authorized_at": "2026-09-02T12:00:00",  # 快照晚于 DB 授权
            },
        )
    )
    token = create_task_token(task_id, instance="i", model_id="m")
    mcp_env.manager.tokens[task_id] = token
    response = call(client, token, task_id, server_id)
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


class FakeCallClient:
    def __init__(self, result, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name, args):
        self.calls.append((name, args))
        if self._error:
            raise self._error
        return self._result

    async def close(self):
        return None


def _stub_call_factory(monkeypatch, client: FakeCallClient):
    from backend.services import mcp_service

    def factory(settings):
        return lambda server: client

    monkeypatch.setattr(mcp_service, "_default_client_factory", factory)


def test_call_executes_and_returns_content(client, mcp_env, monkeypatch):
    task_id, server_id, _ = mcp_env.db.run(seed_task(db_env=mcp_env))
    token = create_task_token(task_id, instance="i", model_id="m")
    mcp_env.manager.tokens[task_id] = token
    fake = FakeCallClient({"content": "a.txt\nb.txt", "is_error": False})
    _stub_call_factory(monkeypatch, fake)
    response = call(client, token, task_id, server_id, args={"path": "/workspace"})
    assert response.status_code == 200
    assert response.json()["data"] == {"content": "a.txt\nb.txt", "is_error": False}
    assert fake.calls == [("list_directory", {"path": "/workspace"})]


def test_call_tool_error_passthrough(client, mcp_env, monkeypatch):
    task_id, server_id, _ = mcp_env.db.run(seed_task(db_env=mcp_env))
    token = create_task_token(task_id, instance="i", model_id="m")
    mcp_env.manager.tokens[task_id] = token
    _stub_call_factory(monkeypatch, FakeCallClient({"content": "路径不存在", "is_error": True}))
    response = call(client, token, task_id, server_id)
    assert response.status_code == 200
    assert response.json()["data"] == {"content": "路径不存在", "is_error": True}


def test_call_upstream_failure_502(client, mcp_env, monkeypatch):
    from backend.engine.mcp_client import MCPClientError

    task_id, server_id, _ = mcp_env.db.run(seed_task(db_env=mcp_env))
    token = create_task_token(task_id, instance="i", model_id="m")
    mcp_env.manager.tokens[task_id] = token
    _stub_call_factory(monkeypatch, FakeCallClient(None, error=MCPClientError("refused")))
    response = call(client, token, task_id, server_id)
    assert response.status_code == 502


def test_call_without_manager_token_401(client, mcp_env):
    """manager 无该任务的令牌（容器未运行/后端重启）→ 401。"""
    task_id, server_id, _ = mcp_env.db.run(seed_task(db_env=mcp_env))
    token = create_task_token(task_id, instance="i", model_id="m")
    response = call(client, token, task_id, server_id)
    assert response.status_code == 401
