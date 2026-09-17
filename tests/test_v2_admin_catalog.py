"""admin 目录管理与 kill switch 端到端测试（Phase 7 T6）。

- HTTP 层经 httpx.AsyncClient(ASGITransport) 驱动真实 app（T2-T4 同款 PG-httpx
  形态；admin_client 直驱全局 app）；
- kill-switch 造轮：**显式导入 tests.test_v2_task_executor 造轮助手**（先例
  login_mfa←verification_flow）+ rt.executor 注入 + admin_client 组合——queued
  任务 aborted(tool_revoked) / running stop_round 断言；
- 种子/复核一律 superuser（pg.engine）；provider_catalog/tool_catalog 无 RLS
  （0001:1025 admin blanket ALL），测试仍走 superuser 统一口径；
- 门序负例（D6）：role 门（非 admin 403 FORBIDDEN，且 user 客户端无 TOTP——
  若②先行会误报 ADMIN_MFA_REQUIRED）→ MFA 时效（回拨 13h 403）→ reason 门 →
  幂等 key 门；kill-switch 限流（admin_kill_switch 10/h）重放不耗窗。
"""

import asyncio
import base64
import threading
import uuid as _uuid

import httpx
import pytest
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.main import app
from backend.v2 import idempotency, provider_service
from backend.v2.runtime import owner_session
from backend.v2.session_service import COOKIE_NAME, create_session
from backend.v2.task_storage import TaskStorage
from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import admin_client
from tests.v2_executor_helpers import (
    _drain_round_tasks,
    _make_executor,
    _seed_task_tool,
    _wait_prompt,
)
from tests.v2_provider_helpers import catalog_id_by_host, seed_provider, seed_task_for_provider
from tests.v2_task_helpers import seed_running_task, seed_task_user

# admin_env 夹具发现形态：pytest 经模块命名空间发现夹具，「import + 参数同名」
# 触发 ruff F811——以赋值别名引入（T1 钉死形态，见 test_v2_admin_invitations.py:34）。
admin_env = _vah.admin_env

# Provider KEK 材料（对齐 v2_provider_helpers._PROVIDER_KEK_MATERIAL；与 admin_env
# 注入的 RATE_LIMIT/MFA 材料互异——D9「多把密钥互不相同」纪律）。
_PROVIDER_KEK = base64.urlsafe_b64encode(bytes(range(32, 64))).decode()
_UA = "AgentCraft-AdminCatalogTest/1.0"
_CATALOG = "/api/admin/catalog"
_KILL = "/api/admin/tools/check_code_style/kill-switch"
_TOOL = ("check_code_style", "1")


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


async def _one(pg, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


async def _scalar(pg, sql: str, params: dict | None = None):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one_or_none()


async def _audit_count(pg, action: str) -> int:
    return await _scalar(pg, "SELECT count(*) FROM audit_logs WHERE action = :a", {"a": action})


async def _user_client(pg, rt, *, email: str) -> httpx.AsyncClient:
    """role='user'（无 TOTP）+ mfa_verified 会话客户端——403 FORBIDDEN 即门序①
    role 门先行的证据（T2-T4 同款）。"""
    uid = str(_uuid.uuid4())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc) "
                "VALUES (:i, :e, 'h', 'user', 'active', NULL)"
            ),
            {"i": uid, "e": email},
        )
    async with owner_session(rt, uid) as db:
        token, csrf = await create_session(
            db, user_id=_uuid.UUID(uid), device_label=_UA, mfa_verified=True
        )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.9.0.7", 51002)),
        base_url="http://testserver",
        headers={"User-Agent": _UA, "X-CSRF-Token": csrf},
        cookies={COOKIE_NAME: token},
    )


async def _rewind_mfa_verified(pg, user_id: str, *, hours: int) -> None:
    """superuser 回拨会话 mfa_verified_at（sessions owner-RLS，superuser 绕过）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET mfa_verified_at = now() - make_interval(secs => :s) "
                "WHERE user_id = CAST(:u AS uuid)"
            ),
            {"s": hours * 3600, "u": user_id},
        )


async def _put_tool(
    client: httpx.AsyncClient,
    *,
    enabled: bool,
    reason: str | None = "演练停用",
    key: str = "k-tool-1",
    tool_id: str = _TOOL[0],
    version: str = _TOOL[1],
    body: dict | None = None,
) -> httpx.Response:
    payload = (
        body
        if body is not None
        else _strip_none(
            {"tool_id": tool_id, "version": version, "enabled": enabled, "reason": reason}
        )
    )
    headers = {} if key is None else {"Idempotency-Key": key}
    return await client.put(f"{_CATALOG}/tools", json=payload, headers=headers)


def _strip_none(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if v is not None}


async def _put_provider(
    client: httpx.AsyncClient,
    cid: str,
    *,
    enabled: bool | None = None,
    models: list[str] | None = None,
    reason: str | None = "目录调整",
    key: str = "k-provider-1",
) -> httpx.Response:
    payload = _strip_none({"enabled": enabled, "models": models, "reason": reason})
    headers = {} if key is None else {"Idempotency-Key": key}
    return await client.put(f"{_CATALOG}/providers/{cid}", json=payload, headers=headers)


async def _kill_switch(
    client: httpx.AsyncClient,
    *,
    version: str = _TOOL[1],
    reason: str = "紧急止血",
    key: str | None = "k-ks-1",
    tool_id: str = _TOOL[0],
) -> httpx.Response:
    headers = {} if key is None else {"Idempotency-Key": key}
    return await client.post(
        f"/api/admin/tools/{tool_id}/kill-switch",
        json={"version": version, "reason": reason},
        headers=headers,
    )


# ---------- GET /catalog/tools ----------


async def test_list_tools_full_shape_and_read_audit_free(pg, admin_env):
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="tools-ls@x.test")
    resp = await client.get(f"{_CATALOG}/tools")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 5  # 0002 种子 tool_catalog×5
    first = data["items"][0]  # tool_id 升序：check_code_style 居首
    assert first["tool_id"] == "check_code_style"
    assert first["version"] == "1"
    assert first["label"] == "check_code_style"  # 控制面常量表 PLATFORM_TOOLS
    assert first["enabled"] is True
    assert first["permissions"] == {"paths": ["/task-files", "/outputs"], "network": False}
    assert set(first.keys()) == {"tool_id", "version", "label", "enabled", "permissions"}
    # 元数据读免审计
    assert await _audit_count(pg, "tool_catalog.set_enabled") == 0
    await client.aclose()


# ---------- PUT /catalog/tools（set_tool_enabled 薄壳）----------


async def test_put_tools_disable_row_values_and_service_audit(pg, admin_env):
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="tools-put@x.test")
    resp = await _put_tool(client, enabled=False)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == {
        "tool_id": "check_code_style",
        "version": "1",
        "enabled": False,
        "already_in_state": False,
    }
    assert (
        await _scalar(
            pg,
            "SELECT enabled FROM tool_catalog WHERE tool_id = :t AND version = :v",
            {"t": _TOOL[0], "v": _TOOL[1]},
        )
        is False
    )
    audit = await _one(
        pg, "SELECT action, detail FROM audit_logs WHERE action = 'tool_catalog.set_enabled'"
    )
    # 审计红线：detail 四键，不落 permissions 整包
    assert audit["detail"] == {
        "tool_id": "check_code_style",
        "version": "1",
        "enabled_before": True,
        "enabled_after": False,
    }
    # 消费门联动：停用后任务面断言拒绝（app 会话可跑——tool_catalog 只读）
    from backend.v2.tool_service import assert_tool_enabled

    async with admin_env.app_factory() as db:
        with pytest.raises(AgentCraftError) as excinfo:
            await assert_tool_enabled(db, _TOOL[0], _TOOL[1])
    assert excinfo.value.code == ErrorCode.TOOL_REVOKED
    await client.aclose()


async def test_put_tools_idempotent_replay_and_reason_conflict(pg, admin_env):
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="tools-idem@x.test")
    first = await _put_tool(client, enabled=False, key="k-tool-replay")
    assert first.status_code == 200
    replay = await _put_tool(client, enabled=False, key="k-tool-replay")
    assert replay.status_code == 200
    assert replay.json() == first.json()  # 原样重放
    conflict = await _put_tool(client, enabled=False, reason="换个理由", key="k-tool-replay")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert await _audit_count(pg, "tool_catalog.set_enabled") == 1  # 重放不重复审计
    await client.aclose()


async def test_put_tools_unknown_tool_404(pg, admin_env):
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="tools-404@x.test")
    resp = await _put_tool(client, enabled=False, tool_id="no_such_tool", key="k-tool-404")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert await _audit_count(pg, "tool_catalog.set_enabled") == 0
    await client.aclose()


# ---------- GET/PUT /catalog/providers ----------


async def test_list_providers_includes_disabled_owner_face_contrast(pg, admin_env):
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="prov-ls@x.test")
    resp = await client.get(f"{_CATALOG}/providers")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 3  # 0002 种子 provider_catalog×3（faux 停用）
    by_host = {item["allowed_host"]: item for item in data["items"]}
    assert by_host["faux.invalid"]["enabled"] is False  # admin 面含停用条目
    openai_item = by_host["api.openai.com"]
    assert openai_item["models"] == ["gpt-4o-mini", "gpt-4o"]
    assert set(openai_item.keys()) == {
        "id",
        "display_name",
        "allowed_host",
        "models",
        "enabled",
    }
    # 对照：owner 面消费视图只含启用条目（过滤语义相反）
    async with admin_env.app_factory() as db:
        owner_items = await provider_service.list_catalog(db)
    assert len(owner_items) == 2
    await client.aclose()


async def test_put_provider_enabled_flip_and_audit(pg, admin_env):
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="prov-flip@x.test")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    resp = await _put_provider(client, cid, enabled=False, key="k-prov-off")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["id"] == cid
    assert data["enabled"] is False
    assert (
        await _scalar(
            pg, "SELECT enabled FROM provider_catalog WHERE id = CAST(:c AS uuid)", {"c": cid}
        )
        is False
    )
    audit = await _one(
        pg, "SELECT action, detail FROM audit_logs WHERE action = 'catalog.provider.update'"
    )
    assert audit["action"] == "catalog.provider.update"
    assert audit["detail"]["enabled_before"] is True
    assert audit["detail"]["enabled_after"] is False
    # 再启用：before/after 翻转，两笔审计
    back = await _put_provider(client, cid, enabled=True, key="k-prov-on")
    assert back.status_code == 200
    assert back.json()["data"]["enabled"] is True
    assert await _audit_count(pg, "catalog.provider.update") == 2
    await client.aclose()


async def test_put_provider_models_whitelist_and_audit(pg, admin_env):
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="prov-models@x.test")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    resp = await _put_provider(client, cid, models=["gpt-4o"], key="k-prov-models")
    assert resp.status_code == 200
    assert resp.json()["data"]["models"] == ["gpt-4o"]
    audit = await _one(pg, "SELECT detail FROM audit_logs WHERE action = 'catalog.provider.update'")
    assert audit["detail"]["models_before"] == ["gpt-4o-mini", "gpt-4o"]
    assert audit["detail"]["models_after"] == ["gpt-4o"]
    await client.aclose()


async def test_put_provider_enabled_and_models_combined_single_audit(pg, admin_env):
    """enabled+models 同请求组合（Phase 8 T7，Progress §5.6 转办）：单事务双变更
    ——两字段同 PUT 原子落位（行值同翻）+ 审计**单行**（detail 四键 before/after
    齐备；_one 用 .one()，若审计落两行即抛 MultipleResultsFound，行数双证）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="prov-combo@x.test")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    resp = await _put_provider(
        client, cid, enabled=False, models=["gpt-4o-mini"], key="k-prov-combo"
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["enabled"] is False
    assert data["models"] == ["gpt-4o-mini"]
    row = await _one(
        pg,
        "SELECT enabled, models FROM provider_catalog WHERE id = CAST(:c AS uuid)",
        {"c": cid},
    )
    assert row == {"enabled": False, "models": ["gpt-4o-mini"]}
    audit = await _one(pg, "SELECT detail FROM audit_logs WHERE action = 'catalog.provider.update'")
    assert audit["detail"] == {
        "provider_id": cid,
        "display_name": "OpenAI",
        "enabled_before": True,
        "enabled_after": False,
        "models_before": ["gpt-4o-mini", "gpt-4o"],
        "models_after": ["gpt-4o-mini"],
    }
    await client.aclose()


async def test_put_provider_whitelist_shrink_no_retroaction(pg, admin_env):
    """白名单收缩不回溯存量：存量 user_providers 行（含模型/状态）零联动。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="prov-shrink@x.test")
    uid = await seed_task_user(pg, "shrink-owner@x.test")
    pid = await seed_provider(pg, uid)  # gpt-4o-mini @ api.openai.com
    cid = await catalog_id_by_host(pg, "api.openai.com")
    resp = await _put_provider(client, cid, models=["gpt-4o"], key="k-prov-shrink")
    assert resp.status_code == 200
    row = await _one(
        pg,
        "SELECT model_id, status FROM user_providers WHERE id = CAST(:p AS uuid)",
        {"p": pid},
    )
    assert row == {"model_id": "gpt-4o-mini", "status": "active"}  # 存量行原样
    await client.aclose()


async def test_put_provider_disabled_no_longer_gates_owner_create_and_test(
    pg, admin_env, monkeypatch
):
    """去目录化翻转（2026-09-17 裁决）：目录 enabled 门退役——停用条目不再阻断
    owner 面 create/test（原 CATALOG_ITEM_DISABLED 联动删除，错误码仅供历史幂等
    重放；provider_catalog 表保留但用户流程不消费）。create 以 base_url 直填载荷
    成功出 ProviderOut 新形态（无 catalog 字段）；connectivity 无目录门，mock 上游
    200 即 ok。"""
    monkeypatch.setenv("PROVIDER_KEY_ENCRYPTION_KEY", _PROVIDER_KEK)  # admin_env 未注入 KEK
    # 出网护栏打桩（provider_service 函数内 import，桩 net_guard 模块属性即生效）
    # ——connectivity 正例零真实网络（test_v2_provider_test_endpoint 同款纪律）
    monkeypatch.setattr(
        "backend.utils.net_guard.assert_public_https", lambda url: None, raising=True
    )
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="prov-gate@x.test")
    uid = await seed_task_user(pg, "gate-owner@x.test")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    resp = await _put_provider(client, cid, enabled=False, key="k-prov-gate")
    assert resp.status_code == 200
    updates = {
        "base_url": "https://api.openai.com/v1",
        "model_id": "gpt-4o-mini",
        "api_key": "sk-test-1234",
        "is_default": False,
    }
    detail = await provider_service.create_provider(
        admin_env,
        user_id=uid,
        updates=updates,
        idem_key="k-create-gate-off",
        idem_hash=idempotency.request_hash(updates),
    )
    assert detail["base_url"] == "https://api.openai.com/v1"
    assert detail["model_id"] == "gpt-4o-mini" and detail["key_last4"] == "1234"
    assert detail["status"] == "active" and detail["is_default"] is False
    assert set(detail) == {
        "id",
        "base_url",
        "model_id",
        "key_last4",
        "key_version",
        "status",
        "is_default",
        "created_at",
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "m1"}]})
    )
    out = await provider_service.test_provider_connectivity(
        admin_env, user_id=uid, provider_id=detail["id"], transport=transport
    )
    assert out["ok"] is True and out["models_visible"] == 1
    assert set(out) == {"ok", "latency_ms", "models_visible"}
    await client.aclose()


async def test_put_provider_missing_404_bad_uuid_and_empty_payload_400(pg, admin_env):
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="prov-bad@x.test")
    missing = await _put_provider(client, str(_uuid.uuid4()), enabled=False, key="k-prov-404")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    bad_uuid = await _put_provider(client, "not-a-uuid", enabled=False, key="k-prov-uuid")
    assert bad_uuid.status_code == 400
    assert bad_uuid.json()["error"]["code"] == "VALIDATION_ERROR"
    empty = await _put_provider(
        client, await catalog_id_by_host(pg, "api.openai.com"), key="k-prov-empty"
    )
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "VALIDATION_ERROR"
    await client.aclose()


# ---------- POST /tools/{tool_id}/kill-switch（D1 例外申报壳）----------


async def test_kill_switch_queued_task_aborted_tool_revoked(pg, admin_env):
    """queued：壳停用提交 → terminator 圈定 → aborted(tool_revoked)+轮取消+释放。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="ks-q@x.test")
    uid = await seed_task_user(pg, "ks-q-owner@x.test")
    pid = await seed_provider(pg, uid)
    tid = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    await _seed_task_tool(pg, tid)
    executor, _streams, _transports = _make_executor(admin_env)
    admin_env.executor = executor
    try:
        resp = await _kill_switch(client, key="k-ks-queued")
    finally:
        admin_env.executor = None
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["tool_id"] == "check_code_style"
    assert data["enabled"] is False
    assert data["already_in_state"] is False
    term = data["termination"]
    assert term["stopped"] == 0
    assert term["aborted_task_ids"] == [tid]
    receipt = term["receipts"][0]
    assert receipt["task_id"] == tid
    assert receipt["status_before"] == "queued"
    assert receipt["flipped"] is True
    # 行值真实变化：任务终态 + 轮取消 + 持有释放
    assert await _one(
        pg, "SELECT status, abort_reason FROM tasks WHERE id = CAST(:t AS uuid)", {"t": tid}
    ) == {
        "status": "aborted",
        "abort_reason": "tool_revoked",
    }
    assert (
        await _scalar(
            pg, "SELECT state FROM task_rounds WHERE task_id = CAST(:t AS uuid)", {"t": tid}
        )
        == "cancelled"
    )
    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = CAST(:t AS uuid) "
            "AND kind = 'active'",
            {"t": tid},
        )
        == "released"
    )
    assert await _audit_count(pg, "tool_catalog.set_enabled") == 1
    await client.aclose()


async def test_kill_switch_running_task_stop_round_and_abort(pg, admin_env, tmp_path):
    """running：executor.stop_round（forced）→ aborted(tool_revoked)+对称释放。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="ks-r@x.test")
    uid = await seed_task_user(pg, "ks-r-owner@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    await _seed_task_tool(pg, tid)
    admin_env.storage = TaskStorage(tmp_path / "task-storage")
    executor, _streams, transports = _make_executor(admin_env)
    transports[0].ignore_abort = True
    transports[0].release = threading.Event()
    await executor.notify(tid)
    run_task = asyncio.create_task(executor.run_pending())
    await _wait_prompt(transports[0])
    admin_env.executor = executor
    try:
        resp = await _kill_switch(client, key="k-ks-running")
    finally:
        admin_env.executor = None
    assert resp.status_code == 200
    term = resp.json()["data"]["termination"]
    assert term["stopped"] == 1
    assert term["aborted_task_ids"] == [tid]
    receipt = term["receipts"][0]
    assert receipt["stop"]["mode"] == "forced"
    assert receipt["stop"]["stopped"] is True
    assert receipt["flipped"] is True
    assert await _one(
        pg, "SELECT status, abort_reason FROM tasks WHERE id = CAST(:t AS uuid)", {"t": tid}
    ) == {
        "status": "aborted",
        "abort_reason": "tool_revoked",
    }
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_reservations WHERE task_id = CAST(:t AS uuid) "
            "AND kind IN ('running','active') AND state = 'released'",
            {"t": tid},
        )
        == 2
    )
    await asyncio.wait_for(run_task, timeout=20)  # 执行链 _EngineDied 收口
    transports[0].release.set()
    await _drain_round_tasks(transports[0])
    await client.aclose()


async def test_kill_switch_executor_none_degrades_disable_only(pg, admin_env):
    """executor None = 只停用不联动降级：termination=None，任务面零动作。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="ks-none@x.test")
    assert admin_env.executor is None
    resp = await _kill_switch(client, key="k-ks-none")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["enabled"] is False
    assert data["termination"] is None
    assert (
        await _scalar(
            pg,
            "SELECT enabled FROM tool_catalog WHERE tool_id = :t AND version = :v",
            {"t": _TOOL[0], "v": _TOOL[1]},
        )
        is False
    )
    await client.aclose()


async def test_kill_switch_idempotent_replay_no_rerun(pg, admin_env):
    """同 key 重放：原样返回提交时刻基线（termination=None），不重跑 terminator、
    不重复审计、不重复耗限流窗。首秀（executor 注入、无圈定任务）termination
    为空集回执；重放（executor 已撤）返回 None 基线——重跑产物必非 None，
    两形态差异即「未重跑」的判别证据。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="ks-replay@x.test")
    executor, _streams, _transports = _make_executor(admin_env)
    admin_env.executor = executor
    try:
        first = await _kill_switch(client, key="k-ks-replay")
    finally:
        admin_env.executor = None
    assert first.status_code == 200
    assert first.json()["data"]["termination"] == {
        "stopped": 0,
        "aborted_task_ids": [],
        "receipts": [],
    }
    replay = await _kill_switch(client, key="k-ks-replay")
    assert replay.status_code == 200
    assert replay.json()["data"]["termination"] is None  # 基线（非重跑产物）
    assert await _audit_count(pg, "tool_catalog.set_enabled") == 1
    assert (
        await _scalar(
            pg, "SELECT count(*) FROM rate_limit_events WHERE scope = 'admin_kill_switch'"
        )
        == 1
    )
    await client.aclose()


async def test_kill_switch_rate_limit_429_and_replay_no_window(pg, admin_env):
    """admin_kill_switch 10/h：第 11 发 429（Retry-After）；重放不耗窗不 429。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="ks-429@x.test")
    for i in range(10):
        resp = await _kill_switch(client, key=f"k-ks-limit-{i}")
        assert resp.status_code == 200, (i, resp.text)
    eleventh = await _kill_switch(client, key="k-ks-limit-over")
    assert eleventh.status_code == 429
    assert eleventh.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert eleventh.headers.get("Retry-After") is not None
    # 已有 key 的重放走幂等命中，先于限流（D6 门序）——不 429、不新增事件行
    replay = await _kill_switch(client, key="k-ks-limit-0")
    assert replay.status_code == 200
    assert (
        await _scalar(
            pg, "SELECT count(*) FROM rate_limit_events WHERE scope = 'admin_kill_switch'"
        )
        == 10
    )
    await client.aclose()


# ---------- 门序负例（D6）----------


async def test_catalog_routes_role_gate_first_403_no_side_effects(pg, admin_env):
    client = await _user_client(pg, admin_env, email="plain@x.test")
    get = await client.get(f"{_CATALOG}/tools")
    put_tool = await _put_tool(client, enabled=False, key="k-denied-1")
    put_provider = await _put_provider(
        client, await catalog_id_by_host(pg, "api.openai.com"), enabled=False, key="k-denied-2"
    )
    kill = await _kill_switch(client, key="k-denied-3")
    for resp in (get, put_tool, put_provider, kill):
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
    assert await _audit_count(pg, "tool_catalog.set_enabled") == 0
    assert await _audit_count(pg, "catalog.provider.update") == 0
    await client.aclose()


async def test_catalog_routes_mfa_window_expired_403(pg, admin_env):
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="stale-mfa@x.test")
    await _rewind_mfa_verified(pg, admin_id, hours=13)
    for resp in (
        await client.get(f"{_CATALOG}/tools"),
        await client.get(f"{_CATALOG}/providers"),
        await _put_tool(client, enabled=False, key="k-stale-1"),
        await _kill_switch(client, key="k-stale-2"),
    ):
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
    await client.aclose()


async def test_catalog_reason_and_idem_key_gates_400(pg, admin_env):
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="gates@x.test")
    no_reason = await _put_tool(client, enabled=False, reason=None, key="k-gate-1")
    assert no_reason.status_code == 400
    assert no_reason.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
    blank = await _put_tool(client, enabled=False, reason="   ", key="k-gate-2")
    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
    no_key = await _put_tool(client, enabled=False, key=None)
    assert no_key.status_code == 400
    assert no_key.json()["error"]["code"] == "VALIDATION_ERROR"
    extra = await _put_tool(
        client,
        enabled=False,
        key="k-gate-3",
        body={
            "tool_id": _TOOL[0],
            "version": _TOOL[1],
            "enabled": False,
            "reason": "r",
            "extra": 1,
        },
    )
    assert extra.status_code == 400
    assert extra.json()["error"]["code"] == "VALIDATION_ERROR"
    ks_no_reason = await _kill_switch(client, reason=None, key="k-gate-4")
    assert ks_no_reason.status_code == 400
    assert ks_no_reason.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
    prov_no_reason = await _put_provider(
        client,
        await catalog_id_by_host(pg, "api.openai.com"),
        enabled=False,
        reason=None,
        key="k-gate-5",
    )
    assert prov_no_reason.status_code == 400
    assert prov_no_reason.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
    assert await _audit_count(pg, "tool_catalog.set_enabled") == 0
    await client.aclose()
