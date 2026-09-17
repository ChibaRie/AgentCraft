"""MCP 任务挂载与 stdio 沙箱发现链（Phase 10 M3+M4）。

- M3：stdio 发现 = subprocess 直拉 + MCP stdio 握手（initialize → tools/list），
  测试内用 python -c 极简 fake MCP server 驱动真实协议往返；失败面统一 502
  MCP_DISCOVER_FAILED（spawn 失败 / 协议错误 / 超时 / 输出超限），子进程纪律
  （10s 超时、2MB 输出上限）经参数注入收窄验证；
- M4：POST /api/tasks 载荷 mcp_refs（≤3）→ 创建事务内校验（存在 + enabled +
  owner RLS）→ tasks.mcp_servers 快照冻结三键描述符（PUT/停用 server 不回写）；
  视图 mcp_servers 键回显；执行链装配纯函数（stdio → 解密启动配置信封 env、
  http → USER_MCP_URL_<n> env、无 mcp-sandbox 镜像跳过路径）。
"""

import json
import logging
import sys
import uuid as _uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from backend.v2 import mcp_service
from backend.v2.models import UserMcpServer
from backend.v2.provider_crypto import key_sealer
from backend.v2.runtime import owner_session
from backend.v2.task_executor import _mcp_http_env, _RoundContext
from tests.v2_provider_helpers import seed_active_user, seed_provider, seed_task_for_provider
from tests.v2_task_helpers import api_env as api_env  # noqa: F401  # re-export fixture
from tests.v2_task_helpers import login_client, seed_login_domain, seed_task_user

_CREATE = "/api/mcp/servers"

_STDIO_CMD = "/usr/local/bin/mcp-server"


# ---------------------------------------------------------------------------
# 基础助手
# ---------------------------------------------------------------------------


async def _create_server(
    client,
    *,
    name="My MCP",
    transport_kind="stdio",
    command=_STDIO_CMD,
    args=None,
    env=None,
    url=None,
    idem="idem-m4-mcp-1",
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
    resp = await client.post(_CREATE, json=body, headers={"Idempotency-Key": idem})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _create_task(client, rid, pid, *, mcp_refs=None, idem="idem-m4-task-1"):
    body = {"expert_revision_id": rid, "initial_message": "x"}
    if pid is not None:
        body["provider_id"] = pid
    if mcp_refs is not None:
        body["mcp_refs"] = mcp_refs
    return await client.post("/api/tasks", json=body, headers={"Idempotency-Key": idem})


async def _db_row(pg, server_id: str) -> dict | None:
    async with pg.engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT * FROM user_mcp_servers WHERE id = :i"),
                    {"i": _uuid.UUID(server_id)},
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
                        "SELECT tool_name, name, description, schema_json FROM user_mcp_tools "
                        "WHERE server_id = :i ORDER BY tool_name"
                    ),
                    {"i": _uuid.UUID(server_id)},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def _stdio_row(runtime, uid: str, server_id: str) -> UserMcpServer:
    async with owner_session(runtime, str(uid)) as db:
        return (
            await db.execute(select(UserMcpServer).where(UserMcpServer.id == _uuid.UUID(server_id)))
        ).scalar_one()


# fake MCP stdio server（python -c 内联）：FAKE_MCP_MODE 控制行为分支
# ok=正常回 tools / error=tools/list 回 JSON-RPC error / silent=tools/list 不回 /
# flood=tools/list 夹带 4MB 填充（输出超限路径）。
_FAKE_MCP_SERVER = "\n".join(
    [
        "import json, os, sys",
        "mode = os.environ.get('FAKE_MCP_MODE', 'ok')",
        "tools = [{'name': 'echo', 'title': 'Echo Tool', 'description': 'echo tool',"
        "          'inputSchema': {'type': 'object'}}]",
        "for line in sys.stdin:",
        "    line = line.strip()",
        "    if not line:",
        "        continue",
        "    try:",
        "        req = json.loads(line)",
        "    except ValueError:",
        "        continue",
        "    method = req.get('method')",
        "    if method == 'initialize':",
        "        resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': "
        "{'protocolVersion': '2025-03-26', 'capabilities': {}, "
        "'serverInfo': {'name': 'fake-mcp', 'version': '0'}}}",
        "    elif method == 'tools/list':",
        "        if mode == 'error':",
        "            resp = {'jsonrpc': '2.0', 'id': req.get('id'), "
        "'error': {'code': -32603, 'message': 'boom'}}",
        "        elif mode == 'silent':",
        "            continue",
        "        elif mode == 'flood':",
        "            resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': "
        "{'tools': tools, 'pad': 'x' * (4 * 1024 * 1024)}}",
        "        else:",
        "            resp = {'jsonrpc': '2.0', 'id': req.get('id'), 'result': {'tools': tools}}",
        "    else:",
        "        continue",
        "    sys.stdout.write(json.dumps(resp) + '\\n')",
        "    sys.stdout.flush()",
    ]
)


# ---------------------------------------------------------------------------
# M3：stdio 发现链（subprocess 直拉 + stdio 握手）
# ---------------------------------------------------------------------------


async def test_discover_stdio_caches_tools(provider_env, pg):
    """stdio 发现全链：解封启动配置 → fake 进程 stdio 握手 → 工具缓存 + 审计；
    出参/缓存零命令材料泄漏。"""
    uid = await seed_active_user(pg, "m3-stdio@example.com")
    client = await login_client(pg, "m3-stdio@example.com")
    data = await _create_server(
        client,
        command=sys.executable,
        args=["-c", _FAKE_MCP_SERVER],
        env={"FAKE_MCP_MODE": "ok"},
    )
    result = await mcp_service.discover_server(provider_env, user_id=str(uid), server_id=data["id"])
    assert result["server_id"] == data["id"]
    assert result["transport_kind"] == "stdio"
    assert result["tools"] == [
        {"tool_name": "echo", "name": "Echo Tool", "description": "echo tool"}
    ]
    rows = await _db_tools(pg, data["id"])
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "echo" and rows[0]["name"] == "Echo Tool"
    assert rows[0]["schema_json"] == {"type": "object"}
    # 命令材料红线：缓存行与审计零明文
    assert sys.executable not in json.dumps(rows)
    async with pg.engine.connect() as conn:
        audits = (
            (
                await conn.execute(
                    text(
                        "SELECT detail FROM audit_logs WHERE action = 'mcp.server.discover' "
                        "AND target_id = :i"
                    ),
                    {"i": _uuid.UUID(data["id"])},
                )
            )
            .mappings()
            .all()
        )
    assert len(audits) == 1
    assert "command" not in json.dumps(audits[0]["detail"])


async def test_discover_stdio_via_api_endpoint(provider_env, pg):
    """端点面：POST /mcp/servers/{id}/discover 对 stdio 同样 200（501 占位已移除）。"""
    await seed_active_user(pg, "m3-stdio-api@example.com")
    client = await login_client(pg, "m3-stdio-api@example.com")
    data = await _create_server(
        client,
        command=sys.executable,
        args=["-c", _FAKE_MCP_SERVER],
        env={"FAKE_MCP_MODE": "ok"},
    )
    resp = await client.post(f"{_CREATE}/{data['id']}/discover")
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["transport_kind"] == "stdio"
    assert [t["tool_name"] for t in body["tools"]] == ["echo"]


async def test_discover_stdio_spawn_failure_502(provider_env, pg):
    """spawn 失败（命令不存在）→ 502 MCP_DISCOVER_FAILED，缓存零残留。"""
    uid = await seed_active_user(pg, "m3-stdio-spawn@example.com")
    client = await login_client(pg, "m3-stdio-spawn@example.com")
    data = await _create_server(client)
    with pytest.raises(HTTPException) as exc_info:
        await mcp_service.discover_server(provider_env, user_id=str(uid), server_id=data["id"])
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "MCP_DISCOVER_FAILED"
    assert await _db_tools(pg, data["id"]) == []


async def test_discover_stdio_protocol_error_502(provider_env, pg):
    """tools/list 回 JSON-RPC error → 502。"""
    uid = await seed_active_user(pg, "m3-stdio-err@example.com")
    client = await login_client(pg, "m3-stdio-err@example.com")
    data = await _create_server(
        client,
        command=sys.executable,
        args=["-c", _FAKE_MCP_SERVER],
        env={"FAKE_MCP_MODE": "error"},
    )
    with pytest.raises(HTTPException) as exc_info:
        await mcp_service.discover_server(provider_env, user_id=str(uid), server_id=data["id"])
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "MCP_DISCOVER_FAILED"


async def test_discover_stdio_timeout_502(provider_env, pg):
    """超时路径（注入 0.5s 收窄验证 10s 缺省纪律的同一形态）→ 502。"""
    uid = await seed_active_user(pg, "m3-stdio-timeout@example.com")
    client = await login_client(pg, "m3-stdio-timeout@example.com")
    data = await _create_server(
        client,
        command=sys.executable,
        args=["-c", _FAKE_MCP_SERVER],
        env={"FAKE_MCP_MODE": "silent"},
    )
    row = await _stdio_row(provider_env, uid, data["id"])
    with pytest.raises(HTTPException) as exc_info:
        await mcp_service.discover_stdio(row, timeout=0.5)
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "MCP_DISCOVER_FAILED"


async def test_discover_stdio_output_cap_502(provider_env, pg):
    """输出超限（单行越流上限 + 累计越 2MB 预算的同一 502 形态）→ 502。"""
    uid = await seed_active_user(pg, "m3-stdio-flood@example.com")
    client = await login_client(pg, "m3-stdio-flood@example.com")
    data = await _create_server(
        client,
        command=sys.executable,
        args=["-c", _FAKE_MCP_SERVER],
        env={"FAKE_MCP_MODE": "flood"},
    )
    row = await _stdio_row(provider_env, uid, data["id"])
    with pytest.raises(HTTPException) as exc_info:
        await mcp_service.discover_stdio(row, max_output_bytes=64 * 1024)
    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "MCP_DISCOVER_FAILED"


# ---------------------------------------------------------------------------
# M4：任务快照挂载（mcp_refs → tasks.mcp_servers 冻结）
# ---------------------------------------------------------------------------


async def test_create_task_mcp_refs_snapshot_frozen(pg, api_env):
    """快照冻结：创建后 PUT（改名+停用）不影响任务快照三键描述符。"""
    _, pid, rid = await seed_login_domain(pg, "m4-frozen@example.com")
    client = await login_client(pg, "m4-frozen@example.com")
    srv = await _create_server(client, name="My MCP")
    resp = await _create_task(client, rid, pid, mcp_refs=[{"server_id": srv["id"]}])
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["data"]["task"]["id"]
    renamed = await client.put(
        f"{_CREATE}/{srv['id']}",
        json={"name": "Renamed", "enabled": False},
        headers={"Idempotency-Key": "idem-m4-put-1"},
    )
    assert renamed.status_code == 200, renamed.text
    view = (await client.get(f"/api/tasks/{task_id}")).json()["data"]["task"]
    assert view["mcp_servers"] == [
        {"server_id": srv["id"], "name": "My MCP", "transport_kind": "stdio"}
    ]


async def test_create_task_snapshot_preserves_ref_order(pg, api_env):
    """多 server 快照按 refs 顺序冻结；http 形态描述符同形。"""
    _, pid, rid = await seed_login_domain(pg, "m4-order@example.com")
    client = await login_client(pg, "m4-order@example.com")
    srv_a = await _create_server(client, name="A", idem="idem-m4-order-a")
    srv_b = await _create_server(
        client,
        name="B",
        transport_kind="http",
        command=None,
        url="https://mcp.example.com",
        idem="idem-m4-order-b",
    )
    resp = await _create_task(
        client,
        rid,
        pid,
        mcp_refs=[{"server_id": srv_b["id"]}, {"server_id": srv_a["id"]}],
        idem="idem-m4-task-order",
    )
    assert resp.status_code == 201, resp.text
    view = resp.json()["data"]["task"]
    assert [m["name"] for m in view["mcp_servers"]] == ["B", "A"]
    assert view["mcp_servers"][0]["transport_kind"] == "http"


async def test_create_task_disabled_server_400(pg, api_env):
    """已停用 server 挂载 → 400 VALIDATION_ERROR。"""
    _, pid, rid = await seed_login_domain(pg, "m4-disabled@example.com")
    client = await login_client(pg, "m4-disabled@example.com")
    srv = await _create_server(client)
    await client.put(
        f"{_CREATE}/{srv['id']}",
        json={"enabled": False},
        headers={"Idempotency-Key": "idem-m4-disable"},
    )
    resp = await _create_task(client, rid, pid, mcp_refs=[{"server_id": srv["id"]}])
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_create_task_cross_user_server_404(pg, api_env):
    """跨用户 server（RLS 0 行）与缺失 server 同形 404 NOT_FOUND。"""
    _, _, _ = await seed_login_domain(pg, "m4-owner@example.com")
    owner_client = await login_client(pg, "m4-owner@example.com")
    srv = await _create_server(owner_client)
    _, _, other_rid = await seed_login_domain(pg, "m4-other@example.com")
    other = await login_client(pg, "m4-other@example.com")
    cross = await _create_task(other, other_rid, None, mcp_refs=[{"server_id": srv["id"]}])
    assert cross.status_code == 404, cross.text
    assert cross.json()["error"]["code"] == "NOT_FOUND"
    missing = await _create_task(
        other,
        other_rid,
        None,
        mcp_refs=[{"server_id": str(_uuid.uuid4())}],
        idem="idem-m4-missing",
    )
    assert missing.status_code == 404
    assert missing.json() == cross.json()


async def test_create_task_refs_cap_400(pg, api_env):
    """>3 个 refs → 400 VALIDATION_ERROR（去重前计数门）。"""
    _, pid, rid = await seed_login_domain(pg, "m4-cap@example.com")
    client = await login_client(pg, "m4-cap@example.com")
    refs = [
        {"server_id": (await _create_server(client, name=f"S{i}", idem=f"idem-m4-cap-{i}"))["id"]}
        for i in range(4)
    ]
    resp = await _create_task(client, rid, pid, mcp_refs=refs, idem="idem-m4-task-cap")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_create_task_duplicate_refs_deduped(pg, api_env):
    """重复 server_id 去重（保序），快照单条目。"""
    _, pid, rid = await seed_login_domain(pg, "m4-dedup@example.com")
    client = await login_client(pg, "m4-dedup@example.com")
    srv = await _create_server(client)
    resp = await _create_task(
        client,
        rid,
        pid,
        mcp_refs=[{"server_id": srv["id"]}, {"server_id": srv["id"]}],
        idem="idem-m4-task-dedup",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["task"]["mcp_servers"] == [
        {"server_id": srv["id"], "name": "My MCP", "transport_kind": "stdio"}
    ]


async def test_create_task_malformed_server_id_400(pg, api_env):
    """非法 server_id 形态 → 400 VALIDATION_ERROR。"""
    _, pid, rid = await seed_login_domain(pg, "m4-badid@example.com")
    client = await login_client(pg, "m4-badid@example.com")
    resp = await _create_task(
        client, rid, pid, mcp_refs=[{"server_id": "z" * 40}], idem="idem-m4-badid"
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_create_task_without_refs_view_exposes_empty_list(pg, api_env):
    """无 mcp_refs 任务视图同样暴露 mcp_servers 键（空列表）。"""
    _, pid, rid = await seed_login_domain(pg, "m4-norefs@example.com")
    client = await login_client(pg, "m4-norefs@example.com")
    resp = await _create_task(client, rid, pid)
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["task"]["mcp_servers"] == []


# ---------------------------------------------------------------------------
# M4：执行链装配纯函数（_build_mcp_attachments / _mcp_http_env）
# ---------------------------------------------------------------------------


def _make_ctx() -> _RoundContext:
    tid, oid, rid, mid = (str(_uuid.uuid4()) for _ in range(4))
    return _RoundContext(
        task_id=tid,
        task_uuid=_uuid.UUID(tid),
        owner_id=oid,
        owner_uuid=_uuid.UUID(oid),
        round_id=rid,
        round_uuid=_uuid.UUID(rid),
        source_message_uuid=_uuid.UUID(mid),
        epoch=1,
        renew_seconds=30.0,
    )


async def test_build_mcp_attachments_stdio_launch_envelope(pg, provider_env, monkeypatch):
    """stdio → mcp-sandbox ContainerSpec：解密启动配置经 env 信封注入（只读数据面）。"""
    from tests.v2_executor_helpers import _make_executor

    executor, _streams, _transports = _make_executor(provider_env)
    monkeypatch.setattr(executor._settings, "MCP_SANDBOX_IMAGE", "agentcraft-mcp-sandbox:test")
    launch = {"command": "node", "args": ["server.js"], "env": {"MCP_TOKEN": "t"}}
    ctx = _make_ctx()
    snapshot = [
        {
            "server_id": str(_uuid.uuid4()),
            "name": "fs",
            "transport_kind": "stdio",
            "launch": launch,
        },
        {
            "server_id": str(_uuid.uuid4()),
            "name": "web",
            "transport_kind": "http",
            "url": "https://mcp.example.com",
        },
    ]
    specs = executor._build_mcp_attachments(ctx, snapshot)
    assert len(specs) == 1  # http 形态不产生附件（URL 走任务容器 env）
    spec = specs[0]
    assert spec.image == "agentcraft-mcp-sandbox:test"
    assert spec.network_name == executor._settings.PI_NETWORK_NAME
    assert spec.mounts == []
    assert spec.env["AGENTCRAFT_MCP_SERVER_ID"] == snapshot[0]["server_id"]
    assert json.loads(spec.env["AGENTCRAFT_MCP_LAUNCH"]) == launch
    assert spec.labels["agentcraft.task_id"] == ctx.task_id
    assert spec.labels["agentcraft.mcp_server_id"] == snapshot[0]["server_id"]
    # 明文命令材料不进容器 argv（env 数据面注入；零宿主文件落盘）
    assert "node" not in json.dumps(spec.argv)


async def test_build_mcp_attachments_no_image_skips(pg, provider_env):
    """mcp-sandbox 镜像未配置（M5 前缺省空串）→ 对应条目 None（跳过不阻塞）。"""
    from tests.v2_executor_helpers import _make_executor

    executor, _streams, _transports = _make_executor(provider_env)
    snapshot = [
        {"server_id": str(_uuid.uuid4()), "name": "fs", "transport_kind": "stdio"},
    ]
    assert executor._build_mcp_attachments(_make_ctx(), snapshot) == [None]
    assert executor._build_mcp_attachments(_make_ctx(), []) == []


def test_mcp_http_env_injection():
    """http 形态 → 任务容器 env USER_MCP_URL_<n>（n 为 http server 0 起序号，
    stdio 条目不占号）。"""
    entries = [
        {"server_id": "a", "transport_kind": "http", "url": "https://a.example.com"},
        {"server_id": "b", "transport_kind": "stdio", "launch": {"command": "node"}},
        {"server_id": "c", "transport_kind": "http", "url": "https://c.example.com"},
    ]
    assert _mcp_http_env(entries) == {
        "USER_MCP_URL_0": "https://a.example.com",
        "USER_MCP_URL_1": "https://c.example.com",
    }
    assert _mcp_http_env([]) == {}
    assert _mcp_http_env([{"server_id": "b", "transport_kind": "stdio"}]) == {}


async def test_build_container_spec_injects_mcp_env(pg, provider_env):
    """主任务容器 spec 合并 mcp_env（http URL 注入面）。"""
    from pathlib import Path

    from tests.v2_executor_helpers import _make_executor

    executor, _streams, _transports = _make_executor(provider_env)
    spec = executor._build_container_spec(
        _make_ctx(),
        system_prompt="p",
        extension_path=Path("/tmp/task.ts"),
        task_token="tok",
        model_id="m",
        mcp_env={"USER_MCP_URL_0": "https://a.example.com"},
    )
    assert spec.env["USER_MCP_URL_0"] == "https://a.example.com"


async def test_round_with_mcp_snapshot_skips_mount_when_no_image(
    pg, provider_env, tmp_path, caplog
):
    """生产路径：快照含 stdio server（真实解密富化）→ 无镜像 WARNING 跳过，
    轮照常完成（不阻塞主流程）。"""
    from backend.v2.task_dispatcher import _instance_id, dispatch_once
    from backend.v2.task_storage import TaskStorage
    from tests.v2_executor_helpers import _make_executor, _run_round, _scalar

    provider_env.storage = TaskStorage(tmp_path / "task-storage")
    uid = str(await seed_task_user(pg, "m4-round@x.test"))
    pid = str(await seed_provider(pg, uid))
    tid = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    # superuser 播种 enabled stdio server（真实信封密文）并冻结进任务快照
    sid = str(_uuid.uuid4())
    sealed, dek = key_sealer().seal(
        json.dumps({"command": "node", "args": ["x.js"], "env": {}}), provider_id=sid
    )
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_mcp_servers (id, owner_id, name, transport_kind, "
                "command_encrypted, command_dek_wrapped, enabled) "
                "VALUES (:i, :u, 'fs', 'stdio', :c, :d, true)"
            ),
            {"i": _uuid.UUID(sid), "u": uid, "c": sealed, "d": dek},
        )
        await conn.execute(
            text("UPDATE tasks SET mcp_servers = :m WHERE id = :t"),
            {
                "m": json.dumps([{"server_id": sid, "name": "fs", "transport_kind": "stdio"}]),
                "t": _uuid.UUID(tid),
            },
        )
    executor, _streams, _transports = _make_executor(provider_env, instance_id=_instance_id())
    provider_env.executor = executor
    with caplog.at_level(logging.WARNING, logger="agentcraft.task.executor"):
        assert await dispatch_once(provider_env) == 1
        assert await _run_round(provider_env, executor, tid) == 1
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "ready"
    assert any("mcp-sandbox" in r.message for r in caplog.records)
