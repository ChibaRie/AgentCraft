"""/internal 回调面测试（Phase 5 收窄后）。

- /internal/mcp/call 已物理删除（用户 MCP 语义下线，T4）；openapi 契约面同步
  收口于 tests/test_api_contracts.py
- /internal/harness/check-code-style：X-Task-Token 三重校验（签名/任务一致/
  实例一致）→ tool_catalog.enabled 第二校验（kill switch，V2 可选运行时依赖）
- /internal/ui/response：501 占位同样先验任务令牌（/internal 不留未鉴权面）

401 路径在任务查询之前即被拦截，故用例无需播种任务行。
"""

import pytest
from sqlalchemy import text

from backend.config import Settings, get_settings
from backend.dependencies import get_pi_engine_manager
from backend.main import app
from backend.services.task_token import create_task_token
from backend.v2.runtime import get_optional_v2_runtime
from tests.test_v2_runtime import make_v2_runtime

pytestmark = pytest.mark.usefixtures("client")


class FakeManager:
    def __init__(self) -> None:
        self.tokens: dict[int, str] = {}

    def get_task_token(self, task_id: int):
        return self.tokens.get(task_id)


class SimpleEnv:
    def __init__(self, manager) -> None:
        self.manager = manager


@pytest.fixture()
def mcp_env(test_db, tmp_path):
    settings = Settings(
        HOST_DATA_ROOT=str(tmp_path / "data"),
        HOST_WORKSPACE_ROOT=str(tmp_path / "workspaces"),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    manager = FakeManager()
    app.dependency_overrides[get_pi_engine_manager] = lambda: manager
    yield SimpleEnv(manager)
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_pi_engine_manager, None)


# ---------------------------------------------------------------------------
# 鉴权：X-Task-Token 三重校验（载体 = /internal/harness/check-code-style）
# ---------------------------------------------------------------------------


def _post_check(client, token, task_id=1):
    return client.post(
        "/internal/harness/check-code-style",
        json={"task_id": task_id, "path": "."},
        headers={"X-Task-Token": token} if token is not None else None,
    )


def test_missing_token_401(client, mcp_env):
    response = _post_check(client, None)
    assert response.status_code == 401


def test_bad_token_401(client, mcp_env):
    response = _post_check(client, "garbage")
    assert response.status_code == 401


def test_token_task_mismatch_401(client, mcp_env):
    task_id = 1
    other_token = create_task_token(task_id + 100, instance="inst-a", model_id="m")
    mcp_env.manager.tokens[task_id] = create_task_token(task_id, instance="inst-a", model_id="m")
    response = _post_check(client, other_token, task_id=task_id)
    assert response.status_code == 401


def test_stale_instance_token_401(client, mcp_env):
    """容器已重建：manager 只认当前实例令牌。"""
    task_id = 1
    stale = create_task_token(task_id, instance="old-instance", model_id="m")
    mcp_env.manager.tokens[task_id] = create_task_token(
        task_id, instance="new-instance", model_id="m"
    )
    response = _post_check(client, stale, task_id=task_id)
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# kill switch 第二校验（tool_catalog.enabled，V2 可选运行时依赖注入）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_code_style_blocked_when_tool_disabled(client, mcp_env, pg):
    """kill switch 第二校验：目录停用 check_code_style@1 后回调 403 TOOL_REVOKED。

    测试进程被 conftest neutralize_v2_env 钉空双 DSN，被测 app 必须经依赖
    override 注入 pg-backed runtime（直调 v2_runtime_from_settings() 恒 None
    会静默跳过校验）。
    """
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_optional_v2_runtime] = lambda: rt
    try:
        token = create_task_token(1, instance="i", model_id="m")
        mcp_env.manager.tokens[1] = token
        async with pg.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tool_catalog SET enabled = false "
                    "WHERE tool_id = 'check_code_style' AND version = '1'"
                )
            )
        resp = _post_check(client, token)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "TOOL_REVOKED"
    finally:
        app.dependency_overrides.pop(get_optional_v2_runtime, None)
        rt.close()


@pytest.mark.asyncio
async def test_check_code_style_proceeds_when_tool_enabled(client, mcp_env, pg):
    """第二校验只拦停用/不存在：目录在场且启用时放行到任务查询（无任务 → 404）。"""
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_optional_v2_runtime] = lambda: rt
    try:
        token = create_task_token(1, instance="i", model_id="m")
        mcp_env.manager.tokens[1] = token
        async with pg.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE tool_catalog SET enabled = true "
                    "WHERE tool_id = 'check_code_style' AND version = '1'"
                )
            )
        resp = _post_check(client, token)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"
    finally:
        app.dependency_overrides.pop(get_optional_v2_runtime, None)
        rt.close()


# ---------------------------------------------------------------------------
# /internal/ui/response：501 占位同样先验任务令牌
# ---------------------------------------------------------------------------


def test_ui_response_without_token_401(client, mcp_env):
    response = client.post("/internal/ui/response", json={"task_id": 1})
    assert response.status_code == 401


def test_ui_response_valid_token_501(client, mcp_env):
    task_id = 1
    token = create_task_token(task_id, instance="i", model_id="m")
    mcp_env.manager.tokens[task_id] = token
    response = client.post(
        "/internal/ui/response",
        json={"task_id": task_id},
        headers={"X-Task-Token": token},
    )
    assert response.status_code == 501
