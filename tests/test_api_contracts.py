import re

import pytest
from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)

CONTRACTS = [
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/users/me"),
    ("POST", "/api/users/me/expert"),
    ("POST", "/api/experts"),
    ("GET", "/api/experts"),
    ("GET", "/api/experts/1"),
    ("PUT", "/api/experts/1"),
    ("POST", "/api/experts/1/publish"),
    ("POST", "/api/experts/1/offline"),
    ("DELETE", "/api/experts/1"),
    ("POST", "/api/experts/1/skills"),
    ("PUT", "/api/experts/1/skills/1"),
    ("DELETE", "/api/experts/1/skills/1"),
    ("GET", "/api/discover/experts"),
    ("GET", "/api/discover/experts/1"),
    ("POST", "/api/skills"),
    ("GET", "/api/skills"),
    ("GET", "/api/skills/1"),
    ("PUT", "/api/skills/1"),
    ("POST", "/api/skills/1/publish"),
    ("POST", "/api/skills/1/offline"),
    ("POST", "/api/skills/1/validate"),
    ("DELETE", "/api/skills/1"),
    ("GET", "/api/workspaces"),
    ("POST", "/api/tasks"),
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/1"),
    ("POST", "/api/tasks/1/files"),
    ("GET", "/api/tasks/1/files"),
    ("POST", "/api/tasks/1/messages"),
    ("POST", "/api/providers"),
    ("GET", "/api/providers"),
    ("GET", "/api/providers/1"),
    ("PUT", "/api/providers/1"),
    ("DELETE", "/api/providers/1"),
    ("POST", "/api/tasks/1/complete"),
    ("POST", "/api/tasks/1/abort"),
    ("DELETE", "/api/tasks/1"),
    ("POST", "/api/mcp/servers"),
    ("GET", "/api/mcp/servers"),
    ("GET", "/api/mcp/servers/1"),
    ("PUT", "/api/mcp/servers/1"),
    ("POST", "/api/mcp/servers/1/discover"),
    ("PUT", "/api/mcp/servers/1/tools/1"),
    ("POST", "/api/mcp/servers/1/publish"),
    ("POST", "/api/mcp/servers/1/offline"),
    ("DELETE", "/api/mcp/servers/1"),
    ("POST", "/api/experts/1/mcp"),
    ("PUT", "/api/experts/1/mcp/1"),
    ("DELETE", "/api/experts/1/mcp/1"),
    ("POST", "/internal/ui/response"),
    ("POST", "/internal/harness/check-code-style"),
]


def test_all_contract_endpoints_exist_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    paths = {re.sub(r"\{[^}]+\}", "*", key): value for key, value in schema["paths"].items()}
    for method, path in CONTRACTS:
        template = "/".join("*" if part.isdigit() else part for part in path.split("/"))
        assert method.lower() in paths[template], f"missing {method} {template}"


# 已实现的端点从 501 占位断言中移除（行为由 tests/test_users.py、tests/test_skills.py 覆盖）
IMPLEMENTED = {
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/users/me"),
    ("POST", "/api/users/me/expert"),
    ("POST", "/api/skills"),
    ("GET", "/api/skills"),
    ("GET", "/api/skills/1"),
    ("PUT", "/api/skills/1"),
    ("POST", "/api/skills/1/publish"),
    ("POST", "/api/skills/1/offline"),
    ("POST", "/api/skills/1/validate"),
    ("DELETE", "/api/skills/1"),
    ("POST", "/api/experts"),
    ("GET", "/api/experts"),
    ("GET", "/api/experts/1"),
    ("PUT", "/api/experts/1"),
    ("POST", "/api/experts/1/publish"),
    ("POST", "/api/experts/1/offline"),
    ("DELETE", "/api/experts/1"),
    ("POST", "/api/experts/1/skills"),
    ("PUT", "/api/experts/1/skills/1"),
    ("DELETE", "/api/experts/1/skills/1"),
    ("GET", "/api/discover/experts"),
    ("GET", "/api/discover/experts/1"),
    ("GET", "/api/workspaces"),
    ("POST", "/api/tasks"),
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/1"),
    ("POST", "/api/tasks/1/files"),
    ("GET", "/api/tasks/1/files"),
    ("POST", "/api/tasks/1/messages"),
    ("POST", "/api/providers"),
    ("GET", "/api/providers"),
    ("GET", "/api/providers/1"),
    ("PUT", "/api/providers/1"),
    ("DELETE", "/api/providers/1"),
    # 阶段 6：MCP 管理（行为由 test_mcp_api 覆盖；/internal/mcp/call 已删，T4）
    ("POST", "/api/mcp/servers"),
    ("GET", "/api/mcp/servers"),
    ("GET", "/api/mcp/servers/1"),
    ("PUT", "/api/mcp/servers/1"),
    ("POST", "/api/mcp/servers/1/discover"),
    ("PUT", "/api/mcp/servers/1/tools/1"),
    ("POST", "/api/mcp/servers/1/publish"),
    ("POST", "/api/mcp/servers/1/offline"),
    ("DELETE", "/api/mcp/servers/1"),
    ("POST", "/api/experts/1/mcp"),
    ("PUT", "/api/experts/1/mcp/1"),
    ("DELETE", "/api/experts/1/mcp/1"),
    # 阶段 6/7：内部接口均已实现或令牌门禁（行为由 test_harness/test_internal_mcp 覆盖）
    ("POST", "/internal/ui/response"),
    ("POST", "/internal/harness/check-code-style"),
}

# 挂载了真实 JWT 依赖的占位端点：匿名请求先被 401 拦截，轮不到 501
PROTECTED_PREFIXES = (
    "/api/experts",
    "/api/skills",
    "/api/mcp/servers",
    "/api/tasks",
    "/api/users",
    "/api/workspaces",
)


@pytest.mark.parametrize(("method", "path"), CONTRACTS)
def test_unimplemented_contract_returns_placeholder_status(method: str, path: str) -> None:
    if (method, path) in IMPLEMENTED:
        pytest.skip("implemented in phase 1; covered by tests/test_users.py")
    kwargs = {}
    if path == "/api/auth/register":
        kwargs["json"] = {
            "username": "placeholder",
            "email": "placeholder@example.com",
            "password": "placeholder",
        }
    elif path == "/api/auth/login":
        kwargs["json"] = {"login": "placeholder", "password": "placeholder"}
    elif method in {"POST", "PUT"} and path != "/api/tasks/1/files":
        kwargs["json"] = {}
    if path == "/api/tasks/1/files":
        kwargs["files"] = [("files", ("placeholder.txt", b"placeholder"))]
    response = client.request(method, path, **kwargs)
    expected_status = 401 if any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES) else 501
    assert response.status_code == expected_status
