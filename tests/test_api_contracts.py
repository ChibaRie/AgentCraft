"""契约路径切换验收（Phase 8 T13 cutover，D3a 定稿）。

D2 全量裁决：V1 应用面物理删除，V2 自 /api/v2 暂挂前缀切至契约路径
（Sup §10.1 路径切换总表）。本文件钉 cutover 接缝的两件事：

- **V1 死亡集**（CONTRACTS，恰 8；双集合与 IMPLEMENTED 同步为独立验收点）：
  register、users/me/expert、experts/{id}/publish、experts/{id}/skills×3、
  skills/{id}/publish、skills/{id}/validate——V1 独有端点在契约路径上必须
  消失（openapi 零路由）。IMPLEMENTED = 死亡集同集 + workspaces（恰 9），
  workspaces 面同批消亡。
- **401 占位断言**（恰 2）：tasks/{id}/complete、abort——契约路径由 V2 真
  路由接位，匿名请求被 V2 认证门拦截（401 SESSION_EXPIRED，取代 V1 时代
  的 501/401 占位语义）。

D3a 定稿数值：CONTRACTS=8 / IMPLEMENTED=9 / 401 断言=2。
"""

import re

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.v2.runtime import get_v2_runtime

client = TestClient(app)

# —— V1 死亡集（恰 8；D3a 定稿——双集合同步为独立验收点，见 test_contract_sets_synced）——
CONTRACTS = [
    ("POST", "/api/auth/register"),
    ("POST", "/api/users/me/expert"),
    ("POST", "/api/experts/1/publish"),
    ("POST", "/api/experts/1/skills"),
    ("PUT", "/api/experts/1/skills/1"),
    ("DELETE", "/api/experts/1/skills/1"),
    ("POST", "/api/skills/1/publish"),
    ("POST", "/api/skills/1/validate"),
]

# —— 同批消亡集合：死亡集 + workspaces（恰 9）——
IMPLEMENTED = [
    *CONTRACTS,
    ("GET", "/api/workspaces"),
]

# —— V2 真路由接位的契约路径（恰 2）：匿名请求被 V2 认证门 401 拦截 ——
GATED_401 = [
    ("POST", "/api/tasks/1/complete"),
    ("POST", "/api/tasks/1/abort"),
]


def _template(path: str) -> str:
    return "/".join("*" if part.isdigit() else part for part in path.split("/"))


def test_contract_sets_synced() -> None:
    """独立验收点：CONTRACTS 恰 8、IMPLEMENTED 恰为同集 + workspaces（恰 9）。"""
    assert len(CONTRACTS) == 8
    assert len(IMPLEMENTED) == 9
    assert set(IMPLEMENTED) - set(CONTRACTS) == {("GET", "/api/workspaces")}


@pytest.mark.parametrize(("method", "path"), IMPLEMENTED)
def test_dead_v1_contracts_absent_from_openapi(method: str, path: str) -> None:
    """V1 死亡集 + workspaces：openapi 零路由（契约路径易主后 V1 语义不得残留）。"""
    schema = client.get("/openapi.json").json()
    paths = {re.sub(r"\{[^}]+\}", "*", key): value for key, value in schema["paths"].items()}
    assert method.lower() not in paths.get(_template(path), {}), (
        f"V1 死亡端点不应存在于契约路径: {method} {path}"
    )


class _NullAdminSession:
    async def __aenter__(self) -> "_NullAdminSession":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def close(self) -> None:
        return None


class _StubRuntime:
    """401 门测试专用桩：仅满足 get_admin_db 的会话工厂形态。

    无 cookie 请求在 resolve_session 之前即 401（get_v2_auth 短路），
    DB 会话永不触碰；从而 401 断言无需 PG 运行时。
    """

    def admin_factory(self) -> _NullAdminSession:
        return _NullAdminSession()


@pytest.mark.parametrize(("method", "path"), GATED_401)
def test_contract_paths_served_by_v2_auth_gate(method: str, path: str) -> None:
    """V2 真路由接位验证：匿名 POST 被 get_v2_auth 401 拦截（SESSION_EXPIRED）。"""
    app.dependency_overrides[get_v2_runtime] = lambda: _StubRuntime()
    try:
        response = client.request(method, path)
    finally:
        app.dependency_overrides.pop(get_v2_runtime, None)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"
