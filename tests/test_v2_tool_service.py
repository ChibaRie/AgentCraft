"""tool_service 测试（裁决 D5：启停原语 / kill_tool 编排 / 泛化校验）。"""

import pytest
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.v2 import tool_service
from tests import v2_admin_helpers as _vah
from tests.test_v2_task_executor import _make_executor
from tests.v2_admin_helpers import admin_client
from tests.v2_provider_helpers import seed_active_user

# admin_env 夹具发现形态：pytest 经模块命名空间发现夹具，「import + 参数同名」
# 触发 ruff F811——以赋值别名引入（T1 钉死形态，见 test_v2_admin_invitations.py:34）。
admin_env = _vah.admin_env

_ADMIN_SEED = {"n": 0}


async def _seed_admin(pg) -> str:
    _ADMIN_SEED["n"] += 1
    return await seed_active_user(pg, f"tool-admin-{_ADMIN_SEED['n']}@x.com")


async def _call(pg, rt, fn, /, **kw):
    """kill_tool 走 runtime 自持事务；set_tool_enabled 用 admin 会话包装。"""
    if fn is tool_service.kill_tool:
        return await fn(rt, **kw)
    async with rt.admin_factory() as db:
        async with db.begin():
            return await fn(db, **kw)


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_set_tool_enabled_flip_and_audit(pg, provider_env):
    admin = await _seed_admin(pg)
    out = await _call(
        pg,
        provider_env,
        tool_service.set_tool_enabled,
        tool_id="check_code_style",
        version="1",
        enabled=False,
        admin_id=admin,
        reason="演练停用",
        request_id="req-1",
    )
    assert out == {
        "tool_id": "check_code_style",
        "version": "1",
        "enabled": False,
        "already_in_state": False,
    }
    async with pg.engine.begin() as conn:
        enabled = (
            await conn.execute(
                text(
                    "SELECT enabled FROM tool_catalog WHERE tool_id='check_code_style' "
                    "AND version='1'"
                )
            )
        ).scalar_one()
        assert enabled is False
        audit = (
            await conn.execute(
                text("SELECT action, detail FROM audit_logs ORDER BY created_at DESC LIMIT 1")
            )
        ).one()
        assert audit.action == "tool_catalog.set_enabled"
        assert audit.detail["enabled_before"] is True
        assert audit.detail["enabled_after"] is False
        assert "permissions" not in audit.detail  # 审计红线：不落 permissions 整包


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_set_tool_enabled_idempotent_no_double_audit(pg, provider_env):
    admin = await _seed_admin(pg)
    await _call(
        pg,
        provider_env,
        tool_service.set_tool_enabled,
        tool_id="check_code_style",
        version="1",
        enabled=False,
        admin_id=admin,
        reason="r",
        request_id=None,
    )
    out = await _call(
        pg,
        provider_env,
        tool_service.set_tool_enabled,
        tool_id="check_code_style",
        version="1",
        enabled=False,
        admin_id=admin,
        reason="r",
        request_id=None,
    )
    assert out["already_in_state"] is True
    async with pg.engine.begin() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM audit_logs WHERE action='tool_catalog.set_enabled'")
            )
        ).scalar_one()
    assert n == 1  # 幂等短路不重复审计


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_set_tool_enabled_requires_reason(pg, provider_env):
    admin = await _seed_admin(pg)
    with pytest.raises(AgentCraftError) as excinfo:
        await _call(
            pg,
            provider_env,
            tool_service.set_tool_enabled,
            tool_id="check_code_style",
            version="1",
            enabled=False,
            admin_id=admin,
            reason="  ",
            request_id=None,
        )
    assert excinfo.value.code == ErrorCode.ADMIN_REASON_REQUIRED


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_set_tool_enabled_unknown_404(pg, provider_env):
    admin = await _seed_admin(pg)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await _call(
            pg,
            provider_env,
            tool_service.set_tool_enabled,
            tool_id="nope",
            version="1",
            enabled=False,
            admin_id=admin,
            reason="r",
            request_id=None,
        )
    assert excinfo.value.status_code == 404


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_kill_tool_orchestration_with_terminator(pg, provider_env):
    admin = await _seed_admin(pg)
    calls = []

    async def fake_terminator(tool_id: str, version: str) -> dict:
        calls.append((tool_id, version))
        return {"stopped": 2, "receipts": ["t-1", "t-2"]}

    out = await _call(
        pg,
        provider_env,
        tool_service.kill_tool,
        tool_id="check_code_style",
        version="1",
        admin_id=admin,
        reason="演练 kill",
        request_id="req-2",
        terminator=fake_terminator,
    )
    assert out["already_in_state"] is False
    assert out["termination"] == {"stopped": 2, "receipts": ["t-1", "t-2"]}
    assert calls == [("check_code_style", "1")]  # 提交后调用（顺序由 await 天然保证）


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_assert_tool_enabled(pg, provider_env):
    admin = await _seed_admin(pg)
    async with provider_env.app_factory() as db:  # app 会话可跑（tool_catalog 只读授权）
        await tool_service.assert_tool_enabled(db, "check_code_style", "1")  # 不抛
    await _call(
        pg,
        provider_env,
        tool_service.set_tool_enabled,
        tool_id="check_code_style",
        version="1",
        enabled=False,
        admin_id=admin,
        reason="r",
        request_id=None,
    )
    async with provider_env.app_factory() as db:
        with pytest.raises(AgentCraftError) as excinfo:
            await tool_service.assert_tool_enabled(db, "check_code_style", "1")
    assert excinfo.value.code == ErrorCode.TOOL_REVOKED
    assert excinfo.value.http_status == 403
    with pytest.raises(AgentCraftError):
        async with provider_env.app_factory() as db:
            await tool_service.assert_tool_enabled(db, "no_such", "1")


# ---------- Phase 7 T6：kill-switch 壳路径 already_in_state 钉证 ----------


async def test_kill_switch_shell_already_in_state_terminator_still_runs(pg, admin_env):
    """钉证对象=壳路径（D11/T6）：已处目标态时 set_tool_enabled 短路不写审计，
    但 terminator 仍执行——回执含 termination 空集幂等（复刻编排体证据）。"""
    rt = admin_env
    client, _csrf, _admin_id = await admin_client(pg, rt, email="ks-pin@x.test")
    put = await client.put(
        "/api/admin/catalog/tools",
        json={"tool_id": "check_code_style", "version": "1", "enabled": False, "reason": "先停用"},
        headers={"Idempotency-Key": "pin-put-1"},
    )
    assert put.status_code == 200
    executor, _streams, _transports = _make_executor(rt)
    rt.executor = executor
    try:
        resp = await client.post(
            "/api/admin/tools/check_code_style/kill-switch",
            json={"version": "1", "reason": "重复 kill（幂等壳路径）"},
            headers={"Idempotency-Key": "pin-ks-1"},
        )
    finally:
        rt.executor = None
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["already_in_state"] is True
    assert data["termination"] == {"stopped": 0, "aborted_task_ids": [], "receipts": []}
    async with pg.engine.begin() as conn:
        n = (
            await conn.execute(
                text("SELECT count(*) FROM audit_logs WHERE action = 'tool_catalog.set_enabled'")
            )
        ).scalar_one()
    assert n == 1  # 短路不写审计：全库仅 PUT 那笔
    await client.aclose()
