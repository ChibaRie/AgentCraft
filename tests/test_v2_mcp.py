"""用户 MCP 面（Phase 10 M1+M2）：迁移 0013 + 注册/管理 CRUD + 发现链。

- 门序与形态沿 Provider/作者面端点范本（Sup §10.11）：登录 → CSRF → Idempotency-Key；
- 密文断言走 DB 直查（superuser 绕 RLS），解密用 KeySealer 真实信封（provider_env
  注入的 KEK 材料 seal/open 互通）；出参零 command/env/url 泄漏；
- 发现 HTTP 行为经服务函数 transport 注入直测（httpx.MockTransport，真实网络零
  依赖；assert_public_https 打桩）；端点层仅测门序（404 / 501 / 限流 429）；
- 迁移 0013 往返经 alembic 子进程对一次性库执行（test_v2_migrations 同款）。
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.v2 import mcp_service
from backend.v2.provider_crypto import key_sealer
from backend.v2.rate_limit import LIMITS, enforce, hmac_subject
from tests.v2_provider_helpers import auth_client, login, seed_active_user

ROOT = Path(__file__).resolve().parents[1]

_CREATE = "/api/mcp/servers"
_STDIO_CMD = "/usr/local/bin/mcp-server"
_STDIO_ARGS = ["--stdio", "--root", "/data"]
_STDIO_ENV = {"MCP_TOKEN": "secret-token-value"}


# ---------------------------------------------------------------------------
# 基础助手
# ---------------------------------------------------------------------------


async def _create(
    client,
    *,
    name="My MCP",
    transport_kind="stdio",
    command=_STDIO_CMD,
    args=None,
    env=None,
    url=None,
    idem="idem-mcp-1",
):
    body = {"name": name, "transport_kind": transport_kind}
    if command is not None:
        body["command"] = command
    if args is not None:
        body["args"] = args
    if env is not None:
        body["env"] = env
    if url is not None:
        body["url"] = url
    return await client.post(_CREATE, json=body, headers={"Idempotency-Key": idem})


async def _create_ok(client, **kwargs) -> dict:
    resp = await _create(client, **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _db_row(pg, server_id: str) -> dict:
    async with pg.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM user_mcp_servers WHERE id = :i"),
                    {"i": uuid.UUID(server_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


async def _db_tools(pg, server_id: str) -> list[dict]:
    async with pg.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT tool_name, name, description, schema_json, enabled "
                        "FROM user_mcp_tools WHERE server_id = :i "
                        "ORDER BY tool_name"
                    ),
                    {"i": uuid.UUID(server_id)},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _seed_tools(pg, server_id: str, count: int = 2) -> None:
    """superuser 播种发现缓存行（绕 RLS 不绕 FK；验证级联删除用）。"""
    async with pg.engine.begin() as conn:
        for i in range(count):
            await conn.execute(
                text(
                    "INSERT INTO user_mcp_tools (id, server_id, tool_name, description, enabled) "
                    "VALUES (gen_random_uuid(), :s, :n, 'seeded', true)"
                ),
                {"s": uuid.UUID(server_id), "n": f"seed_tool_{i}"},
            )


async def _audit_rows(pg, action: str) -> list[dict]:
    async with pg.engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT action, target_type, detail FROM audit_logs WHERE action = :a"),
                    {"a": action},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 迁移 0013：upgrade/downgrade/upgrade 往返 + 结构断言
# ---------------------------------------------------------------------------


def _run_alembic(dsn: str, *args: str) -> None:
    env = {**os.environ, "AGENTCRAFT_V2_DATABASE_URL": dsn}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic_v2.ini", *args],
        cwd=ROOT,
        env=env,
        check=True,
    )


async def _create_db(base: str, name: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await eng.dispose()


async def _drop_db(base: str, name: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await eng.dispose()


async def _check_0013_objects(dsn: str, expect_present: bool) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(dsn)
    async with eng.connect() as conn:
        tables = (
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "AND table_name IN ('user_mcp_servers', 'user_mcp_tools')"
                    )
                )
            )
            .scalars()
            .all()
        )
        policies = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies WHERE tablename IN "
                    "('user_mcp_servers', 'user_mcp_tools')"
                )
            )
        ).scalar_one()
        app_grants = (
            await conn.execute(
                text(
                    "SELECT count(DISTINCT table_name) FROM information_schema.role_table_grants "
                    "WHERE grantee = 'agentcraft_app' AND table_schema = 'public' "
                    "AND table_name IN ('user_mcp_servers', 'user_mcp_tools')"
                )
            )
        ).scalar_one()
    await eng.dispose()
    if expect_present:
        assert set(tables) == {"user_mcp_servers", "user_mcp_tools"}
        assert policies == 10  # 每表 app 四 policy + admin_read
        assert app_grants == 2
    else:
        assert tables == []
        assert policies == 0
        assert app_grants == 0


def test_0013_upgrade_downgrade_upgrade_cycle(pg_url_base):
    name = "ac_m13_" + uuid.uuid4().hex[:8]
    dsn = f"{pg_url_base}/{name}"
    asyncio.run(_create_db(pg_url_base, name))
    try:
        _run_alembic(dsn, "upgrade", "head")
        asyncio.run(_check_0013_objects(dsn, expect_present=True))
        _run_alembic(dsn, "downgrade", "0012")
        asyncio.run(_check_0013_objects(dsn, expect_present=False))
        _run_alembic(dsn, "upgrade", "head")
        asyncio.run(_check_0013_objects(dsn, expect_present=True))
    finally:
        asyncio.run(_drop_db(pg_url_base, name))


# ---------------------------------------------------------------------------
# 创建（POST /api/mcp/servers）
# ---------------------------------------------------------------------------


async def test_create_stdio_seals_command_envelope(provider_env, pg):
    """stdio：command+args+env 整体 JSON 经信封加密落库；DB 无明文；出参零密文。"""
    uid = await seed_active_user(pg, "mcp-create@example.com")
    async with auth_client() as client:
        await login(client, "mcp-create@example.com", "User-Passw0rd!")
        data = await _create_ok(client, args=_STDIO_ARGS, env=_STDIO_ENV)
    assert set(data.keys()) == {
        "id",
        "name",
        "transport_kind",
        "enabled",
        "has_command",
        "created_at",
    }
    assert data["name"] == "My MCP"
    assert data["transport_kind"] == "stdio"
    assert data["enabled"] is True
    assert data["has_command"] is True
    # 出参零命令材料：键集上面已锁死六键，这里再验明文值不出现
    assert _STDIO_CMD not in json.dumps(data)
    assert "secret-token-value" not in json.dumps(data)

    row = await _db_row(pg, data["id"])
    assert row["command_encrypted"] and row["command_dek_wrapped"]
    assert row["url"] is None
    assert _STDIO_CMD not in row["command_encrypted"]
    assert "secret-token-value" not in row["command_encrypted"]
    assert json.loads(row["command_encrypted"])["alg"] == "A256GCM"
    assert json.loads(row["command_dek_wrapped"])["alg"] == "A256GCM"
    plain = key_sealer().open(
        row["command_encrypted"], row["command_dek_wrapped"], provider_id=data["id"]
    )
    assert json.loads(plain) == {"command": _STDIO_CMD, "args": _STDIO_ARGS, "env": _STDIO_ENV}

    audits = await _audit_rows(pg, "mcp.server.register")
    assert len(audits) == 1
    detail = audits[0]["detail"]
    assert detail["server_id"] == data["id"] and detail["transport_kind"] == "stdio"
    assert "command" not in detail and "env" not in detail
    del uid


async def test_create_http_stores_url(provider_env, pg):
    """http：url 明文落库但不出参；has_command=False。"""
    await seed_active_user(pg, "mcp-http@example.com")
    async with auth_client() as client:
        await login(client, "mcp-http@example.com", "User-Passw0rd!")
        data = await _create_ok(
            client, transport_kind="http", command=None, url="https://mcp.example.com/sse"
        )
    assert data["transport_kind"] == "http" and data["has_command"] is False
    assert "mcp.example.com" not in json.dumps(data) and _STDIO_CMD not in json.dumps(data)
    row = await _db_row(pg, data["id"])
    assert row["url"] == "https://mcp.example.com/sse"
    assert row["command_encrypted"] is None


async def test_create_requires_idempotency_key(provider_env, pg):
    await seed_active_user(pg, "mcp-nokey@example.com")
    async with auth_client() as client:
        await login(client, "mcp-nokey@example.com", "User-Passw0rd!")
        resp = await client.post(
            _CREATE, json={"name": "x", "transport_kind": "stdio", "command": _STDIO_CMD}
        )
    assert resp.status_code == 400


async def test_create_replay_returns_original(provider_env, pg):
    await seed_active_user(pg, "mcp-replay@example.com")
    async with auth_client() as client:
        await login(client, "mcp-replay@example.com", "User-Passw0rd!")
        first = await _create_ok(client, idem="idem-mcp-rp")
        replay = await _create(client, idem="idem-mcp-rp")
        assert replay.status_code == 200
        assert replay.json()["data"] == first
    async with pg.engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM user_mcp_servers"))).scalar_one()
    assert n == 1


async def test_create_validation_matrix(provider_env, pg):
    """形态门：transport 与载荷交叉校验 / 空白名 / extra 字段 / 非 https url。"""
    await seed_active_user(pg, "mcp-valid@example.com")
    async with auth_client() as client:
        await login(client, "mcp-valid@example.com", "User-Passw0rd!")
        cases = [
            {"name": "x", "transport_kind": "stdio"},  # stdio 缺 command
            {
                "name": "x",
                "transport_kind": "stdio",
                "command": _STDIO_CMD,
                "url": "https://mcp.example.com",
            },  # stdio 带 url
            {"name": "x", "transport_kind": "http"},  # http 缺 url
            {
                "name": "x",
                "transport_kind": "http",
                "url": "https://mcp.example.com",
                "command": _STDIO_CMD,
            },  # http 带 command
            {
                "name": "x",
                "transport_kind": "http",
                "url": "https://mcp.example.com",
                "env": {"A": "b"},
            },  # http 带 env
            {"name": "   ", "transport_kind": "stdio", "command": _STDIO_CMD},  # 空白名
            {
                "name": "x",
                "transport_kind": "stdio",
                "command": _STDIO_CMD,
                "extra": 1,
            },  # extra=forbid
            {"name": "x", "transport_kind": "http", "url": "http://mcp.example.com"},  # 非 https
            {
                "name": "x",
                "transport_kind": "http",
                "url": "https://user:pass@mcp.example.com",
            },  # userinfo
        ]
        for i, body in enumerate(cases):
            resp = await client.post(
                _CREATE, json=body, headers={"Idempotency-Key": f"idem-bad-{i}"}
            )
            assert resp.status_code == 400, f"case {i}: {resp.text}"
            assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    async with pg.engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM user_mcp_servers"))).scalar_one()
    assert n == 0


async def test_create_unauthenticated_401(provider_env, pg):
    async with auth_client() as client:
        resp = await _create(client)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 列表（GET /api/mcp/servers）
# ---------------------------------------------------------------------------


async def test_list_shape_and_no_leak(provider_env, pg):
    await seed_active_user(pg, "mcp-list@example.com")
    async with auth_client() as client:
        await login(client, "mcp-list@example.com", "User-Passw0rd!")
        empty = await client.get(_CREATE)
        assert empty.status_code == 200 and empty.json()["data"] == []
        await _create_ok(client, args=_STDIO_ARGS, env=_STDIO_ENV)
        await _create_ok(
            client,
            transport_kind="http",
            command=None,
            url="https://mcp.example.com",
            idem="idem-mcp-list-2",
        )
        resp = await client.get(_CREATE)
    assert resp.status_code == 200
    items = resp.json()["data"]
    assert len(items) == 2
    for item in items:
        assert set(item.keys()) == {
            "id",
            "name",
            "transport_kind",
            "enabled",
            "has_command",
            "created_at",
        }
    body_text = resp.text
    assert _STDIO_CMD not in body_text
    assert "secret-token-value" not in body_text
    assert "mcp.example.com" not in body_text


# ---------------------------------------------------------------------------
# 更新（PUT /api/mcp/servers/{id}）
# ---------------------------------------------------------------------------


async def test_update_three_state(provider_env, pg):
    """PUT 三态：缺席=不变、字符串/布尔=覆盖；name/enabled 独立或同时。"""
    await seed_active_user(pg, "mcp-put@example.com")
    async with auth_client() as client:
        await login(client, "mcp-put@example.com", "User-Passw0rd!")
        data = await _create_ok(client)

        async def _put(payload, idem):
            resp = await client.put(
                f"{_CREATE}/{data['id']}", json=payload, headers={"Idempotency-Key": idem}
            )
            assert resp.status_code == 200, resp.text
            return resp.json()["data"]

        renamed = await _put({"name": "Renamed"}, "idem-put-1")
        assert renamed["name"] == "Renamed" and renamed["enabled"] is True
        disabled = await _put({"enabled": False}, "idem-put-2")
        assert disabled["name"] == "Renamed" and disabled["enabled"] is False
        both = await _put({"name": "Again", "enabled": True}, "idem-put-3")
        assert both["name"] == "Again" and both["enabled"] is True
        noop = await _put({}, "idem-put-4")
        assert noop["name"] == "Again" and noop["enabled"] is True

    row = await _db_row(pg, data["id"])
    assert row["name"] == "Again" and row["enabled"] is True
    audits = await _audit_rows(pg, "mcp.server.update")
    assert len(audits) == 4


async def test_update_missing_and_cross_user_404_same_shape(provider_env, pg):
    uid_a = await seed_active_user(pg, "mcp-put-a@example.com")
    await seed_active_user(pg, "mcp-put-b@example.com")
    async with auth_client() as client:
        await login(client, "mcp-put-a@example.com", "User-Passw0rd!")
        data = await _create_ok(client)
        foreign = uuid.uuid4()
        missing = await client.put(
            f"{_CREATE}/{foreign}", json={"name": "x"}, headers={"Idempotency-Key": "idem-404-1"}
        )
        del uid_a
    async with auth_client() as client:
        await login(client, "mcp-put-b@example.com", "User-Passw0rd!")
        cross = await client.put(
            f"{_CREATE}/{data['id']}", json={"name": "x"}, headers={"Idempotency-Key": "idem-404-2"}
        )
    assert missing.status_code == 404 and cross.status_code == 404
    assert missing.json() == cross.json()
    assert missing.json()["error"]["code"] == "NOT_FOUND"


async def test_update_explicit_null_400(provider_env, pg):
    await seed_active_user(pg, "mcp-put-null@example.com")
    async with auth_client() as client:
        await login(client, "mcp-put-null@example.com", "User-Passw0rd!")
        data = await _create_ok(client)
        resp = await client.put(
            f"{_CREATE}/{data['id']}",
            json={"name": None},
            headers={"Idempotency-Key": "idem-null-1"},
        )
    assert resp.status_code == 400


async def test_update_requires_idempotency_key(provider_env, pg):
    await seed_active_user(pg, "mcp-put-nokey@example.com")
    async with auth_client() as client:
        await login(client, "mcp-put-nokey@example.com", "User-Passw0rd!")
        data = await _create_ok(client)
        resp = await client.put(f"{_CREATE}/{data['id']}", json={"name": "x"})
    assert resp.status_code == 400


async def test_update_replay_returns_original(provider_env, pg):
    await seed_active_user(pg, "mcp-put-rp@example.com")
    async with auth_client() as client:
        await login(client, "mcp-put-rp@example.com", "User-Passw0rd!")
        data = await _create_ok(client)
        first = await client.put(
            f"{_CREATE}/{data['id']}",
            json={"name": "Once"},
            headers={"Idempotency-Key": "idem-put-rp"},
        )
        replay = await client.put(
            f"{_CREATE}/{data['id']}",
            json={"name": "Once"},
            headers={"Idempotency-Key": "idem-put-rp"},
        )
    assert replay.status_code == first.status_code
    assert replay.json() == first.json()


# ---------------------------------------------------------------------------
# 删除（DELETE /api/mcp/servers/{id}）：物理删 + 级联 + 审计
# ---------------------------------------------------------------------------


async def test_delete_physical_cascade_and_audit(provider_env, pg):
    await seed_active_user(pg, "mcp-del@example.com")
    async with auth_client() as client:
        await login(client, "mcp-del@example.com", "User-Passw0rd!")
        data = await _create_ok(client)
        await _seed_tools(pg, data["id"], count=2)
        resp = await client.delete(
            f"{_CREATE}/{data['id']}", headers={"Idempotency-Key": "idem-del-1"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"deleted": True, "id": data["id"]}
    assert await _db_row(pg, data["id"]) is None  # 物理删
    assert await _db_tools(pg, data["id"]) == []  # 工具缓存级联清
    audits = await _audit_rows(pg, "mcp.server.delete")
    assert len(audits) == 1
    assert audits[0]["detail"]["server_id"] == data["id"]


async def test_delete_missing_and_cross_user_404_same_shape(provider_env, pg):
    await seed_active_user(pg, "mcp-del-a@example.com")
    await seed_active_user(pg, "mcp-del-b@example.com")
    async with auth_client() as client:
        await login(client, "mcp-del-a@example.com", "User-Passw0rd!")
        data = await _create_ok(client)
        missing = await client.delete(
            f"{_CREATE}/{uuid.uuid4()}", headers={"Idempotency-Key": "idem-del-404-1"}
        )
    async with auth_client() as client:
        await login(client, "mcp-del-b@example.com", "User-Passw0rd!")
        cross = await client.delete(
            f"{_CREATE}/{data['id']}", headers={"Idempotency-Key": "idem-del-404-2"}
        )
    assert missing.status_code == 404 and cross.status_code == 404
    assert missing.json() == cross.json()


async def test_delete_replay_after_physical_delete(provider_env, pg):
    """物理删后同 key 重放 200 原响应（§7 重放先于 404 门）。"""
    await seed_active_user(pg, "mcp-del-rp@example.com")
    async with auth_client() as client:
        await login(client, "mcp-del-rp@example.com", "User-Passw0rd!")
        data = await _create_ok(client)
        first = await client.delete(
            f"{_CREATE}/{data['id']}", headers={"Idempotency-Key": "idem-del-rp"}
        )
        replay = await client.delete(
            f"{_CREATE}/{data['id']}", headers={"Idempotency-Key": "idem-del-rp"}
        )
    assert first.status_code == 200 and replay.status_code == 200
    assert replay.json() == first.json()


async def test_delete_requires_idempotency_key(provider_env, pg):
    await seed_active_user(pg, "mcp-del-nokey@example.com")
    async with auth_client() as client:
        await login(client, "mcp-del-nokey@example.com", "User-Passw0rd!")
        data = await _create_ok(client)
        resp = await client.delete(f"{_CREATE}/{data['id']}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 发现（POST /api/mcp/servers/{id}/discover）
# ---------------------------------------------------------------------------


_TOOLS_A = [
    {
        "name": "read_file",
        "title": "Read File",
        "description": "读文件",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
    {"name": "write_file", "description": "写文件", "inputSchema": {"type": "object"}},
]
_TOOLS_B = [
    {"name": "search", "title": "Search", "description": "搜索", "inputSchema": {}},
]


def _jsonrpc_ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _mcp_mock_handler(tools, *, capture: list | None = None):
    """MockTransport handler：initialize → notifications/initialized → tools/list。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if capture is not None:
            capture.append(payload.get("method"))
        if payload.get("method") == "initialize":
            return httpx.Response(
                200,
                headers={"mcp-session-id": "sess-42"},
                json=_jsonrpc_ok(
                    payload.get("id"),
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "mock", "version": "0"},
                    },
                ),
            )
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if payload.get("method") == "tools/list":
            assert request.headers.get("mcp-session-id") == "sess-42"
            return httpx.Response(200, json=_jsonrpc_ok(payload.get("id"), {"tools": tools}))
        return httpx.Response(400, json=_jsonrpc_ok(payload.get("id"), {}))

    return handler


async def _discover_service(rt, uid, server_id, handler, monkeypatch):
    monkeypatch.setattr("backend.utils.net_guard.assert_public_https", lambda url: None)
    return await mcp_service.discover_server(
        rt, user_id=str(uid), server_id=server_id, transport=httpx.MockTransport(handler)
    )


async def test_discover_http_caches_tools(provider_env, pg, monkeypatch):
    uid = await seed_active_user(pg, "mcp-disc@example.com")
    async with auth_client() as client:
        await login(client, "mcp-disc@example.com", "User-Passw0rd!")
        data = await _create_ok(
            client, transport_kind="http", command=None, url="https://mcp.example.com"
        )
    result = await _discover_service(
        provider_env, uid, data["id"], _mcp_mock_handler(_TOOLS_A), monkeypatch
    )
    assert result["server_id"] == data["id"]
    assert [t["tool_name"] for t in result["tools"]] == ["read_file", "write_file"]
    rows = await _db_tools(pg, data["id"])
    assert [r["tool_name"] for r in rows] == ["read_file", "write_file"]
    assert rows[0]["name"] == "Read File" and rows[0]["description"] == "读文件"
    assert rows[0]["schema_json"] == _TOOLS_A[0]["inputSchema"]
    assert rows[1]["name"] == "write_file"  # 无 title 回退 tool_name

    # 先删后插幂等：二次发现工具集变化 → 缓存整体替换，不残留
    result2 = await _discover_service(
        provider_env, uid, data["id"], _mcp_mock_handler(_TOOLS_B), monkeypatch
    )
    assert [t["tool_name"] for t in result2["tools"]] == ["search"]
    rows2 = await _db_tools(pg, data["id"])
    assert [r["tool_name"] for r in rows2] == ["search"]

    audits = await _audit_rows(pg, "mcp.server.discover")
    assert len(audits) == 2
    assert all("url" not in a["detail"] and "command" not in a["detail"] for a in audits)


async def test_discover_sse_response_parsed(provider_env, pg, monkeypatch):
    """上游以 text/event-stream 返回 JSON-RPC 亦可解析（streamable HTTP 形态）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("method") == "initialize":
            return httpx.Response(200, json=_jsonrpc_ok(payload.get("id"), {"capabilities": {}}))
        if payload.get("method") == "notifications/initialized":
            return httpx.Response(202)
        body = (
            "event: message\n"
            f"data: {json.dumps(_jsonrpc_ok(payload.get('id'), {'tools': _TOOLS_A}))}\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    uid = await seed_active_user(pg, "mcp-sse@example.com")
    async with auth_client() as client:
        await login(client, "mcp-sse@example.com", "User-Passw0rd!")
        data = await _create_ok(
            client, transport_kind="http", command=None, url="https://mcp.example.com"
        )
    result = await _discover_service(provider_env, uid, data["id"], handler, monkeypatch)
    assert [t["tool_name"] for t in result["tools"]] == ["read_file", "write_file"]


async def test_discover_stdio_501(provider_env, pg):
    """stdio 发现链占位 501（mcp-sandbox 沙箱镜像属 Phase 10 M5）。"""
    await seed_active_user(pg, "mcp-stdio@example.com")
    async with auth_client() as client:
        await login(client, "mcp-stdio@example.com", "User-Passw0rd!")
        data = await _create_ok(client)
        resp = await client.post(f"{_CREATE}/{data['id']}/discover")
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "NOT_IMPLEMENTED"


async def test_discover_missing_and_cross_user_404(provider_env, pg):
    await seed_active_user(pg, "mcp-disc-a@example.com")
    await seed_active_user(pg, "mcp-disc-b@example.com")
    async with auth_client() as client:
        await login(client, "mcp-disc-a@example.com", "User-Passw0rd!")
        data = await _create_ok(
            client, transport_kind="http", command=None, url="https://mcp.example.com"
        )
        missing = await client.post(f"{_CREATE}/{uuid.uuid4()}/discover")
    async with auth_client() as client:
        await login(client, "mcp-disc-b@example.com", "User-Passw0rd!")
        cross = await client.post(f"{_CREATE}/{data['id']}/discover")
    assert missing.status_code == 404 and cross.status_code == 404
    assert missing.json() == cross.json()


async def test_discover_rate_limited_10_per_hour(provider_env, pg):
    """限流 10/h/用户（沿 provider_test 口径）：第 11 次 429 + Retry-After。"""
    uid = await seed_active_user(pg, "mcp-rl@example.com")
    for _ in range(10):
        async with provider_env.app_factory() as db:
            await enforce(db, scope="mcp_discover", subjects=[hmac_subject("user", str(uid))])
    async with auth_client() as client:
        await login(client, "mcp-rl@example.com", "User-Passw0rd!")
        data = await _create_ok(
            client, transport_kind="http", command=None, url="https://mcp.example.com"
        )
        resp = await client.post(f"{_CREATE}/{data['id']}/discover")
    assert resp.status_code == 429
    assert "retry-after" in {k.lower() for k in resp.headers}


async def test_mcp_discover_scope_registered():
    """限流 scope 登记钉测试（沿 admin_kill_switch 先例）。"""
    assert LIMITS["mcp_discover"] == (10, 3600)


async def test_discover_upstream_failure_502(provider_env, pg, monkeypatch):
    """上游连接失败 → 502 MCP_DISCOVER_FAILED，不泄上游细节；缓存零残留。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    uid = await seed_active_user(pg, "mcp-fail@example.com")
    async with auth_client() as client:
        await login(client, "mcp-fail@example.com", "User-Passw0rd!")
        data = await _create_ok(
            client, transport_kind="http", command=None, url="https://mcp.example.com"
        )
    with pytest.raises(HTTPException) as exc_info:
        await _discover_service(provider_env, uid, data["id"], handler, monkeypatch)
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "MCP_DISCOVER_FAILED"
    assert await _db_tools(pg, data["id"]) == []


async def test_discover_private_target_502(provider_env, pg):
    """SSRF 硬约束：出网前 assert_public_https 拒内网目标 → 502（零细节泄漏）。"""
    uid = await seed_active_user(pg, "mcp-ssrf@example.com")
    async with auth_client() as client:
        await login(client, "mcp-ssrf@example.com", "User-Passw0rd!")
        data = await _create_ok(
            client, transport_kind="http", command=None, url="https://10.0.0.5:8080"
        )
    with pytest.raises(HTTPException) as exc_info:
        await mcp_service.discover_server(provider_env, user_id=str(uid), server_id=data["id"])
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "MCP_DISCOVER_FAILED"
    assert "10.0.0.5" not in json.dumps(exc_info.value.detail)
