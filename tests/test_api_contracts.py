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
    ("POST", "/internal/mcp/call"),
    ("POST", "/internal/ui/response"),
    ("POST", "/internal/harness/check-code-style"),
]


def test_all_contract_endpoints_exist_in_openapi() -> None:
    schema = client.get("/openapi.json").json()
    paths = {re.sub(r"\{[^}]+\}", "*", key): value for key, value in schema["paths"].items()}
    for method, path in CONTRACTS:
        template = "/".join("*" if part.isdigit() else part for part in path.split("/"))
        assert method.lower() in paths[template], f"missing {method} {template}"


@pytest.mark.parametrize(("method", "path"), CONTRACTS)
def test_unimplemented_contract_returns_501(method: str, path: str) -> None:
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
    assert response.status_code == 501
