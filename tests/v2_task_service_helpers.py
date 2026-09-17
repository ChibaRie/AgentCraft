"""任务服务测试共享助手（Phase 9 T6 拆分自 test_v2_task_service.py）。

app_engine/domain 夹具 + _snapshot/_create_ok/_seed_uploading_with_files/_scalar/
_user_active_tasks——被 test_v2_task_service（创建/冻结输入）、
test_v2_task_service_lifecycle（complete/abort/delete）、
test_v2_task_service_views（视图/配额/释放）共同消费。
"""

import pytest
from sqlalchemy import text

from backend.v2.task_service import (
    create_task,
)
from tests.conftest import APP_ROLE
from tests.v2_provider_helpers import seed_provider
from tests.v2_task_helpers import (
    owner_tx,
    seed_input_file,
    seed_published_revision,
    seed_task_user,
)

_D14_VIEW_KEYS = {
    "id",
    "status",
    "abort_reason",
    "created_at",
    "input_committed",
    "input_manifest_sha256",
    "event_sequence",
    "active_round",
    "initial_round",
    "counts",
    "expert",
    "provider",
    # Phase 10 M4：用户 MCP 挂载快照回显键
    "mcp_servers",
}
_FAKE_CATALOG_ID = "00000000-0000-0000-0000-00000000000c"


@pytest.fixture
async def app_engine(role_engine):
    engine = role_engine(APP_ROLE)
    yield engine
    await engine.dispose()


@pytest.fixture
async def domain(pg):
    """种子 user + BYOK provider + published revision（含 revision_tools）；返回
    (uid, pid, revision_id, catalog_id)——2026-09-17 去目录化后 catalog_id 为
    None（provider 行不再挂目录）。"""
    uid = await seed_task_user(pg, "t3-user@x.test")
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid)
    async with pg.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT catalog_id FROM user_providers WHERE id = :p"),
                {"p": str(pid)},
            )
        ).first()
    cid = str(row[0]) if row is not None and row[0] is not None else None
    return uid, pid, rid, cid


def _snapshot(pid, cid) -> dict:
    """D14 快照键（与 ResolvedProvider 一一对应）；provider_catalog_id 仅在
    真有目录行时进快照（2026-09-17 去目录化）。"""
    snapshot = {
        "provider_id": str(pid),
        "provider_model_id": "gpt-4o-mini",
        "provider_key_version": 1,
    }
    if cid:
        snapshot["provider_catalog_id"] = str(cid)
    return snapshot


async def _create_ok(db, uid, pid, rid, cid, message="第一句话"):
    return await create_task(
        db,
        owner_id=uid,
        expert_revision_id=rid,
        provider_id=pid,
        initial_message=message,
        provider_snapshot=_snapshot(pid, cid),
    )


async def _seed_uploading_with_files(pg, app_engine, domain, *, sizes=(100, 250)):
    """经真实 create_task 建任务（含初始消息），再 superuser 种 staged 输入文件；
    返回 (task_id, manifest)。"""
    uid, pid, rid, cid = domain
    async with owner_tx(app_engine, uid) as db:
        out = await _create_ok(db, uid, pid, rid, cid)
    tid = out["task"]["id"]
    for i, n in enumerate(sizes):
        await seed_input_file(pg, uid, tid, size_bytes=n, file_name=f"f{i}.txt")
    manifest = [
        {"file_name": f"f{i}.txt", "sha256": "c" * 64, "size": n} for i, n in enumerate(sizes)
    ]
    return tid, manifest


async def _scalar(pg, sql, params=None):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _user_active_tasks(pg, uid):
    return await _scalar(
        pg, "SELECT active_tasks FROM user_quota_usage WHERE user_id = :u", {"u": uid}
    )
