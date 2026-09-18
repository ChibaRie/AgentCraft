"""/internal/mcp/call 用户 MCP 工具回调测试（Phase 10 M5）。

校验链（全序）：decode+登记表（401）→ epoch fence（401）→ 任务快照能力上限
（403）→ server enabled（403）→ 工具缓存 enabled（403）→ 分派执行（stdio
一次性沙箱 / http 直连，失败 502）。测试以 monkeypatch 替换分派层，不触 Docker。
"""

import json
import uuid as _uuid

from sqlalchemy import text

from backend.v2 import mcp_service
from tests.test_internal_tools import _seed_running_minimal
from tests.test_internal_tools import tools_env as tools_env  # noqa: F401  # re-export


def _post_call(client, tid, token, *, server_id, tool_name="echo", arguments=None):
    return client.post(
        "/internal/mcp/call",
        json={
            "task_id": tid,
            "server_id": server_id,
            "tool_name": tool_name,
            "arguments": arguments or {},
        },
        headers={"X-Task-Token": token} if token is not None else None,
    )


async def _seed_server_and_mount(
    pg, uid, tid, *, transport_kind="stdio", enabled=True, tool_enabled=True
):
    sid = str(_uuid.uuid4())
    async with pg.engine.begin() as conn:
        if transport_kind == "stdio":
            await conn.execute(
                text(
                    "INSERT INTO user_mcp_servers (id, owner_id, name, transport_kind, "
                    "command_encrypted, command_dek_wrapped, enabled) "
                    "VALUES (:i, :u, 'fs', 'stdio', 'ct', 'dw', :e)"
                ),
                {"i": _uuid.UUID(sid), "u": uid, "e": enabled},
            )
        else:
            await conn.execute(
                text(
                    "INSERT INTO user_mcp_servers (id, owner_id, name, transport_kind, url, "
                    "enabled) VALUES (:i, :u, 'web', 'http', 'https://mcp.example.com', :e)"
                ),
                {"i": _uuid.UUID(sid), "u": uid, "e": enabled},
            )
        await conn.execute(
            text(
                "INSERT INTO user_mcp_tools (id, server_id, tool_name, name, description, "
                "schema_json, enabled) VALUES (gen_random_uuid(), :s, 'echo', 'Echo', "
                "'echo tool', :sch, :te)"
            ),
            {"s": _uuid.UUID(sid), "sch": json.dumps({"type": "object"}), "te": tool_enabled},
        )
        await conn.execute(
            text("UPDATE tasks SET mcp_servers = :m WHERE id = :t"),
            {
                "m": json.dumps(
                    [{"server_id": sid, "name": "fs", "transport_kind": transport_kind}]
                ),
                "t": _uuid.UUID(tid),
            },
        )
    return sid


async def test_mcp_call_success(client, tools_env, pg, monkeypatch):
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "m5-call@x.test")
    sid = await _seed_server_and_mount(pg, uid, tid)
    token = tools_env.register(uid, tid, rid, epoch)
    called = {}

    async def fake_call(row, *, tool_name, arguments, transport=None):
        called["server_id"] = str(row.id)
        called["tool_name"] = tool_name
        called["arguments"] = arguments
        return {"content": "pong", "is_error": False}

    monkeypatch.setattr(mcp_service, "call_user_mcp_tool", fake_call)
    resp = _post_call(client, tid, token, server_id=sid, arguments={"x": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"content": "pong", "is_error": False}
    assert called == {"server_id": sid, "tool_name": "echo", "arguments": {"x": 1}}


async def test_mcp_call_not_in_snapshot_403(client, tools_env, pg):
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "m5-snap@x.test")
    token = tools_env.register(uid, tid, rid, epoch)
    resp = _post_call(client, tid, token, server_id=str(_uuid.uuid4()))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "TOOL_REVOKED"


async def test_mcp_call_server_disabled_403(client, tools_env, pg):
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "m5-disabled@x.test")
    sid = await _seed_server_and_mount(pg, uid, tid, enabled=False)
    token = tools_env.register(uid, tid, rid, epoch)
    resp = _post_call(client, tid, token, server_id=sid)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "TOOL_REVOKED"


async def test_mcp_call_tool_disabled_403(client, tools_env, pg):
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "m5-tool@x.test")
    sid = await _seed_server_and_mount(pg, uid, tid, tool_enabled=False)
    token = tools_env.register(uid, tid, rid, epoch)
    resp = _post_call(client, tid, token, server_id=sid)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "TOOL_REVOKED"


async def test_mcp_call_fenced_epoch_401(client, tools_env, pg):
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "m5-fence@x.test")
    sid = await _seed_server_and_mount(pg, uid, tid)
    token = tools_env.register(uid, tid, rid, epoch)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE task_rounds SET lease_epoch = lease_epoch + 1 WHERE id = :r"),
            {"r": rid},
        )
    resp = _post_call(client, tid, token, server_id=sid)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


async def test_mcp_call_bad_signature_401(client, tools_env, pg):
    uid, tid, rid, epoch = await _seed_running_minimal(pg, "m5-sig@x.test")
    sid = await _seed_server_and_mount(pg, uid, tid)
    resp = _post_call(client, tid, "not-a-token", server_id=sid)
    assert resp.status_code == 401
