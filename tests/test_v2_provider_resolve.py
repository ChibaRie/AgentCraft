"""resolve_task_provider 测试（裁决 D17；Phase 6 复用接口冻结）。

在 owner_session 事务内直调（Phase 6 将在任务创建事务内传入已设 GUC 的 db）。
"""

import pytest
from fastapi import HTTPException

# noqa: F401 —— brief 预留（需要时用），当前未引用
from sqlalchemy import text as _text  # noqa: F401

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.provider_service import ResolvedProvider, resolve_task_provider
from backend.v2.runtime import owner_session
from tests.test_v2_runtime import make_v2_runtime
from tests.v2_provider_helpers import (
    catalog_id_by_host,
    seed_active_user,
    seed_provider,
)

_FOREIGN_PID = "0197ffff-7fff-7fff-7fff-ffffffffffff"


async def test_explicit_provider_resolves(pg):
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "r1@x.test")
        pid = await seed_provider(pg, uid, catalog_host="api.openai.com", model_id="gpt-4o-mini")
        async with owner_session(rt, str(uid)) as db:
            out = await resolve_task_provider(db, user_id=str(uid), provider_id=str(pid))
        assert isinstance(out, ResolvedProvider)
        assert out.provider_id == str(pid) and out.model_id == "gpt-4o-mini"
        assert out.key_version == 1
        assert out.catalog_id == str(await catalog_id_by_host(pg, "api.openai.com"))
    finally:
        rt.close()


async def test_explicit_missing_404(pg):
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "r2@x.test")
        async with owner_session(rt, str(uid)) as db:
            with pytest.raises(HTTPException) as exc_info:
                await resolve_task_provider(db, user_id=str(uid), provider_id=_FOREIGN_PID)
        assert exc_info.value.status_code == 404
    finally:
        rt.close()


async def test_explicit_revoked_404(pg):
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "r3@x.test")
        pid = await seed_provider(pg, uid, status="revoked")
        async with owner_session(rt, str(uid)) as db:
            with pytest.raises(HTTPException) as exc_info:
                await resolve_task_provider(db, user_id=str(uid), provider_id=str(pid))
        assert exc_info.value.status_code == 404
    finally:
        rt.close()


async def test_explicit_bad_uuid_400(pg):
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "r4@x.test")
        async with owner_session(rt, str(uid)) as db:
            with pytest.raises(HTTPException) as exc_info:
                await resolve_task_provider(db, user_id=str(uid), provider_id="not-a-uuid")
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["code"] == "VALIDATION_ERROR"
    finally:
        rt.close()


async def test_explicit_catalog_disabled_400(pg):
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "r5@x.test")
        pid = await seed_provider(pg, uid, catalog_host="faux.invalid", model_id="faux-echo")
        async with owner_session(rt, str(uid)) as db:
            with pytest.raises(AgentCraftError) as exc_info:
                await resolve_task_provider(db, user_id=str(uid), provider_id=str(pid))
        assert exc_info.value.code == ErrorCode.CATALOG_ITEM_DISABLED
        assert exc_info.value.http_status == 400
    finally:
        rt.close()


async def test_explicit_model_off_whitelist_400(pg):
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "r6@x.test")
        pid = await seed_provider(pg, uid, catalog_host="api.openai.com", model_id="deepseek-chat")
        async with owner_session(rt, str(uid)) as db:
            with pytest.raises(AgentCraftError) as exc_info:
                await resolve_task_provider(db, user_id=str(uid), provider_id=str(pid))
        assert exc_info.value.code == ErrorCode.MODEL_NOT_ALLOWED
    finally:
        rt.close()


async def test_default_fallback_ok(pg):
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "r7@x.test")
        pid = await seed_provider(pg, uid, is_default=True)
        async with owner_session(rt, str(uid)) as db:
            out = await resolve_task_provider(db, user_id=str(uid), provider_id=None)
        assert out.provider_id == str(pid)
    finally:
        rt.close()


async def test_no_default_400_provider_not_configured(pg):
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "r8@x.test")
        async with owner_session(rt, str(uid)) as db:
            with pytest.raises(AgentCraftError) as exc_info:
                await resolve_task_provider(db, user_id=str(uid), provider_id=None)
        assert exc_info.value.code == ErrorCode.PROVIDER_NOT_CONFIGURED
        assert exc_info.value.http_status == 400
    finally:
        rt.close()


async def test_default_but_only_revoked_400(pg):
    """默认位只剩 revoked 行（is_default 已被 D6 清除）→ PROVIDER_NOT_CONFIGURED。"""
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "r9@x.test")
        await seed_provider(pg, uid, is_default=True, status="revoked")
        async with owner_session(rt, str(uid)) as db:
            with pytest.raises(AgentCraftError) as exc_info:
                await resolve_task_provider(db, user_id=str(uid), provider_id=None)
        assert exc_info.value.code == ErrorCode.PROVIDER_NOT_CONFIGURED
    finally:
        rt.close()
