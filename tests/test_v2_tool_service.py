"""tool_service 测试（裁决 D5：启停原语 / kill_tool 编排 / 泛化校验）。"""

import pytest
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.v2 import tool_service
from tests.v2_provider_helpers import seed_active_user

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
