"""任务服务：创建与输入冻结（Phase 6；Phase 9 T6 拆分）。

全事务写链、配额（active/daily/续期/并发无超卖）、工具 kill-switch 首闸、
未发布 revision 拒收；commit_input 成功/二次/终态/存储超限。
生命周期面见 test_v2_task_service_lifecycle.py；视图/配额/释放面见
test_v2_task_service_views.py；共享助手见 v2_task_service_helpers。
"""

import asyncio
import hashlib

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.config import get_settings
from backend.errors import AgentCraftError, ErrorCode
from backend.v2.content_hash import canonical_json
from backend.v2.task_service import (
    commit_input,
)
from tests import v2_task_service_helpers as _ts_helpers
from tests.conftest import APP_ROLE
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import (
    owner_tx,
    seed_input_file,
    seed_published_revision,
    seed_task_user,
)
from tests.v2_task_service_helpers import (
    _create_ok,
    _scalar,
    _seed_uploading_with_files,
    _user_active_tasks,
)

# 夹具发现形态：以赋值别名引入（admin_env 同款惯例）。
app_engine = _ts_helpers.app_engine
domain = _ts_helpers.domain

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
}
_FAKE_CATALOG_ID = "00000000-0000-0000-0000-00000000000c"


async def test_create_task_full_transaction(pg, app_engine, domain):
    """验收基线：uploading 落库 + 初始消息/message_saved 同 sequence + active
    reservation + 配额计数 +1 + 当日 usage_daily。"""
    uid, pid, rid, cid = domain
    async with owner_tx(app_engine, uid) as db:
        out = await _create_ok(db, uid, pid, rid, cid)
    view = out["task"]
    assert view["status"] == "uploading"
    assert view["input_committed"] is False
    assert view["input_manifest_sha256"] is None
    assert view["event_sequence"] == 1
    assert view["active_round"] is None
    assert view["initial_round"] is None
    assert view["counts"] == {"inputs": 0, "outputs": 0}
    assert out["message"]["event_sequence"] == 1

    async with pg.engine.connect() as conn:
        task = (
            (
                await conn.execute(
                    text(
                        "SELECT id, status, initial_message_id, provider_catalog_id, "
                        "provider_model_id, provider_key_version, event_sequence, abort_reason "
                        "FROM tasks WHERE owner_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
        msg = (
            (
                await conn.execute(
                    text(
                        "SELECT id, event_sequence, author, content FROM task_messages "
                        "WHERE task_id = :t"
                    ),
                    {"t": task["id"]},
                )
            )
            .mappings()
            .one()
        )
        ev = (
            (
                await conn.execute(
                    text(
                        "SELECT type, sequence, payload_json, message_id FROM task_events "
                        "WHERE task_id = :t"
                    ),
                    {"t": task["id"]},
                )
            )
            .mappings()
            .one()
        )
        res = (
            (
                await conn.execute(
                    text("SELECT kind, state, bytes FROM task_reservations WHERE task_id = :t"),
                    {"t": task["id"]},
                )
            )
            .mappings()
            .one()
        )
        usage = (
            (
                await conn.execute(
                    text(
                        "SELECT active_tasks, running_tasks, retained_storage_bytes "
                        "FROM user_quota_usage WHERE user_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
        daily = (
            await conn.execute(
                text(
                    "SELECT tasks_started FROM usage_daily WHERE user_id = :u "
                    "AND day = CURRENT_DATE"
                ),
                {"u": uid},
            )
        ).scalar_one()

    assert task["status"] == "uploading" and task["abort_reason"] is None
    assert task["initial_message_id"] == msg["id"]  # 循环 FK 已回填
    assert task["provider_catalog_id"] is None  # 去目录化：provider 行不挂目录
    assert task["provider_model_id"] == "gpt-4o-mini"
    assert task["provider_key_version"] == 1
    assert task["event_sequence"] == 1
    # message/event 关联同一 event sequence（DB §4.2:4）
    assert msg["event_sequence"] == 1 and msg["author"] == "user"
    assert msg["content"] == "第一句话"
    assert ev["type"] == "message_saved" and ev["sequence"] == 1
    assert ev["message_id"] == msg["id"]
    assert ev["payload_json"] == {
        "message_id": str(msg["id"]),
        "event_sequence": 1,
        "author": "user",
    }
    assert res["kind"] == "active" and res["state"] == "held" and res["bytes"] == 0
    assert usage["active_tasks"] == 1 and usage["running_tasks"] == 0
    assert usage["retained_storage_bytes"] == 0
    assert daily == 1


async def test_create_task_rejects_blank_message(pg, app_engine, domain):
    """非空白门（D7d）：400 VALIDATION_ERROR；事务回滚零残留。"""
    uid, pid, rid, cid = domain
    with pytest.raises(HTTPException) as ei:
        async with owner_tx(app_engine, uid) as db:
            await _create_ok(db, uid, pid, rid, cid, message="   \n\t ")
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "VALIDATION_ERROR"
    assert await _scalar(pg, "SELECT count(*) FROM tasks") == 0
    assert await _scalar(pg, "SELECT count(*) FROM usage_daily") == 0


async def test_create_task_message_budget_boundary(pg, app_engine, domain):
    """预算门对读 Settings.SKILL_PROMPT_MAX_BYTES（D7d，65,536）：恰在预算内放行、
    超 1 字节 400。"""
    uid, pid, rid, cid = domain
    limit = get_settings().SKILL_PROMPT_MAX_BYTES
    assert limit == 65536  # D7d 对读钉值
    exact = "好" * (limit // 3) + "a"  # 21845*3 + 1 = 65536 字节
    assert len(exact.encode("utf-8")) == limit
    async with owner_tx(app_engine, uid) as db:
        out = await _create_ok(db, uid, pid, rid, cid, message=exact)
    assert out["task"]["status"] == "uploading"
    with pytest.raises(HTTPException) as ei:
        async with owner_tx(app_engine, uid) as db:
            await _create_ok(db, uid, pid, rid, cid, message=exact + "b")  # 65537 字节
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "VALIDATION_ERROR"


async def test_create_task_quota_active_rejected(pg, app_engine):
    """active 维条件增 rowcount=0 → QUOTA_ACTIVE_EXCEEDED 429；回滚零残留。"""
    uid = await seed_task_user(pg, "t3-quota-a@x.test", max_active_tasks=0)
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid)
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await _create_ok(db, uid, pid, rid, _FAKE_CATALOG_ID)
    assert ei.value.code is ErrorCode.QUOTA_ACTIVE_EXCEEDED
    assert ei.value.http_status == 429
    assert await _user_active_tasks(pg, uid) == 0
    assert await _scalar(pg, "SELECT count(*) FROM usage_daily") == 0
    assert await _scalar(pg, "SELECT count(*) FROM tasks") == 0


async def test_create_task_quota_daily_rejected(pg, app_engine):
    """当日 usage_daily 已满 → QUOTA_DAILY_EXCEEDED 429（冲突路径 WHERE 门）。"""
    uid = await seed_task_user(pg, "t3-quota-d@x.test", max_daily_tasks=5)
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO usage_daily (id, user_id, day, tasks_started) "
                "VALUES (gen_random_uuid(), :u, CURRENT_DATE, 5)"
            ),
            {"u": uid},
        )
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await _create_ok(db, uid, pid, rid, _FAKE_CATALOG_ID)
    assert ei.value.code is ErrorCode.QUOTA_DAILY_EXCEEDED
    assert ei.value.http_status == 429


async def test_create_task_daily_renewal_next_day(pg, app_engine):
    """usage_daily 跨日翻新：昨日满额不阻塞今日（新行 tasks_started=1）。"""
    uid = await seed_task_user(pg, "t3-quota-r@x.test", max_daily_tasks=5)
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO usage_daily (id, user_id, day, tasks_started) "
                "VALUES (gen_random_uuid(), :u, CURRENT_DATE - 1, 5)"
            ),
            {"u": uid},
        )
    async with owner_tx(app_engine, uid) as db:
        out = await _create_ok(db, uid, pid, rid, _FAKE_CATALOG_ID, message="新的一天")
    assert out["task"]["status"] == "uploading"
    async with pg.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT day, tasks_started FROM usage_daily WHERE user_id = :u ORDER BY day"),
                {"u": uid},
            )
        ).all()
    assert len(rows) == 2
    assert rows[0][1] == 5  # 昨日行原样
    assert rows[1][1] == 1  # 今日新行


async def test_create_task_concurrent_no_oversell(pg, role_engine):
    """验收①：双 role_engine 并发创建（owner 级 advisory 锁串行化）——sorted 后
    恰 1 成功 1 QUOTA_ACTIVE，quota 行精确对账（active_tasks=1、daily=1）。"""
    uid = await seed_task_user(pg, "t3-conc@x.test", max_active_tasks=1)
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid)
    e1 = role_engine(APP_ROLE)
    e2 = role_engine(APP_ROLE)
    try:

        async def one(engine):
            async with owner_tx(engine, uid) as db:
                out = await _create_ok(db, uid, pid, rid, _FAKE_CATALOG_ID, message="并发创建")
                return out["task"]["id"]

        results = await asyncio.gather(one(e1), one(e2), return_exceptions=True)
    finally:
        await e1.dispose()
        await e2.dispose()
    ordered = sorted(results, key=lambda r: r if isinstance(r, str) else f"ERR:{r.code.value}")
    assert isinstance(ordered[0], str)  # 恰一成功
    assert isinstance(ordered[1], AgentCraftError)
    assert ordered[1].code is ErrorCode.QUOTA_ACTIVE_EXCEEDED
    assert ordered[1].http_status == 429
    assert await _scalar(pg, "SELECT count(*) FROM tasks") == 1
    assert await _user_active_tasks(pg, uid) == 1
    assert (
        await _scalar(
            pg,
            "SELECT tasks_started FROM usage_daily WHERE user_id = :u AND day = CURRENT_DATE",
            {"u": uid},
        )
        == 1
    )
    assert await _scalar(pg, "SELECT count(*) FROM task_reservations") == 1


async def test_create_task_tool_revoked_first_gate(pg, app_engine):
    """第一时点工具校验（D4）：revision_tools 任一停用 → TOOL_REVOKED 409；
    事务回滚零残留（配额/任务/事件）。"""
    uid = await seed_task_user(pg, "t3-tool@x.test")
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid, tools=(("check_code_style", "1"),))
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tool_catalog SET enabled = false WHERE tool_id = 'check_code_style'")
        )
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await _create_ok(db, uid, pid, rid, _FAKE_CATALOG_ID)
    assert ei.value.code is ErrorCode.TOOL_REVOKED
    assert ei.value.http_status == 409
    assert await _scalar(pg, "SELECT count(*) FROM tasks") == 0
    assert await _scalar(pg, "SELECT count(*) FROM usage_daily") == 0
    assert await _user_active_tasks(pg, uid) == 0


async def test_create_task_unpublished_revision_rejected(pg, app_engine, domain):
    """revision published 门（DB §4.1:4）：archived 不可被新任务选择 →
    REVISION_NOT_PUBLISHED 400。"""
    uid, pid, rid, cid = domain
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE expert_revisions SET status = 'archived' WHERE id = :r"), {"r": rid}
        )
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await _create_ok(db, uid, pid, rid, cid)
    assert ei.value.code is ErrorCode.REVISION_NOT_PUBLISHED
    assert ei.value.http_status == 400


# ---------------------------------------------------------------------------
# commit_input：输入冻结
# ---------------------------------------------------------------------------


async def test_commit_input_success(pg, app_engine, domain):
    """冻结全事务：manifest canonical hash、staged→committed 翻转、task_root
    (bytes=Σ)、双存储账条件增、pending 初始轮、queued + 两事件（D14 commit 形状）。"""
    uid, _pid, _rid, _cid = domain
    tid, manifest = await _seed_uploading_with_files(pg, app_engine, domain)
    async with owner_tx(app_engine, uid) as db:
        out = await commit_input(db, owner_id=uid, task_id=tid, manifest=manifest)
    expected_sha = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    assert out["manifest_sha256"] == expected_sha
    assert out["task"]["status"] == "queued"
    assert out["task"]["input_committed"] is True
    assert out["task"]["input_manifest_sha256"] == expected_sha
    assert out["event_sequence"] == 3  # message_saved(1) → status_changed(2) → round_queued(3)
    assert out["task"]["initial_round"] == {"id": out["round_id"], "state": "pending", "attempt": 0}
    assert out["task"]["active_round"] == out["task"]["initial_round"]

    async with pg.engine.connect() as conn:
        files = (
            (
                await conn.execute(
                    text("SELECT state FROM task_files WHERE task_id = :t ORDER BY file_name"),
                    {"t": tid},
                )
            )
            .scalars()
            .all()
        )
        res = (
            await conn.execute(
                text(
                    "SELECT kind, bytes, state FROM task_reservations WHERE task_id = :t "
                    "ORDER BY kind"
                ),
                {"t": tid},
            )
        ).all()
        rnd = (
            (
                await conn.execute(
                    text(
                        "SELECT id, state, attempt, source_message_id FROM task_rounds "
                        "WHERE task_id = :t"
                    ),
                    {"t": tid},
                )
            )
            .mappings()
            .one()
        )
        init_msg = (
            await conn.execute(
                text("SELECT initial_message_id FROM tasks WHERE id = :t"), {"t": tid}
            )
        ).scalar_one()
        evs = (
            await conn.execute(
                text(
                    "SELECT type, sequence, payload_json FROM task_events WHERE task_id = :t "
                    "ORDER BY sequence"
                ),
                {"t": tid},
            )
        ).all()
        user_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM user_quota_usage WHERE user_id = :u"),
                {"u": uid},
            )
        ).scalar_one()
        platform_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM platform_storage WHERE singleton")
            )
        ).scalar_one()

    assert list(files) == ["committed", "committed"]
    assert {(k, b, s) for k, b, s in res} == {("active", 0, "held"), ("task_root", 350, "held")}
    assert rnd["state"] == "pending" and rnd["attempt"] == 0
    assert rnd["source_message_id"] == init_msg  # 初始轮 source = 初始消息
    assert str(rnd["id"]) == out["round_id"]
    assert [e[0] for e in evs] == ["message_saved", "status_changed", "round_queued"]
    assert [e[1] for e in evs] == [1, 2, 3]
    assert evs[1][2] == {"status": "queued"}
    assert evs[2][2] == {"round_id": out["round_id"]}
    assert user_bytes == 350 and platform_bytes == 350


async def test_commit_input_twice_rejected(pg, app_engine, domain):
    """验收②：二次 commit → INPUT_COMMITTED 409；状态/事件/账目零增量。"""
    uid, _pid, _rid, _cid = domain
    tid, manifest = await _seed_uploading_with_files(pg, app_engine, domain)
    async with owner_tx(app_engine, uid) as db:
        await commit_input(db, owner_id=uid, task_id=tid, manifest=manifest)
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await commit_input(db, owner_id=uid, task_id=tid, manifest=manifest)
    assert ei.value.code is ErrorCode.INPUT_COMMITTED
    assert ei.value.http_status == 409
    assert await _scalar(pg, "SELECT count(*) FROM task_rounds WHERE task_id = :t", {"t": tid}) == 1
    assert await _scalar(pg, "SELECT count(*) FROM task_events WHERE task_id = :t", {"t": tid}) == 3
    assert (
        await _scalar(
            pg, "SELECT retained_storage_bytes FROM user_quota_usage WHERE user_id = :u", {"u": uid}
        )
        == 350
    )
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_reservations WHERE task_id = :t AND kind = 'task_root'",
            {"t": tid},
        )
        == 1
    )


async def test_commit_input_terminal_state_rejected(pg, app_engine, domain):
    """终态任务的 commit → TASK_INVALID_TRANSITION 409（非法态分支）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tasks SET status = 'failed', abort_reason = 'upload_expired' WHERE id = :t"
            ),
            {"t": tid},
        )
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await commit_input(db, owner_id=uid, task_id=tid, manifest=[])
    assert ei.value.code is ErrorCode.TASK_INVALID_TRANSITION
    assert ei.value.http_status == 409


async def test_commit_input_storage_exceeded(pg, app_engine):
    """用户存储维条件增 rowcount=0 → QUOTA_STORAGE_EXCEEDED 429；回滚零残留
    （文件仍 staged、无 task_root、双账不动、无轮无增量事件）。"""
    uid = await seed_task_user(pg, "t3-store@x.test", max_retained_storage_bytes=10)
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid)
    async with owner_tx(app_engine, uid) as db:
        out = await _create_ok(db, uid, pid, rid, _FAKE_CATALOG_ID)
    tid = out["task"]["id"]
    await seed_input_file(pg, uid, tid, size_bytes=100, file_name="big.bin")
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await commit_input(
                db,
                owner_id=uid,
                task_id=tid,
                manifest=[{"file_name": "big.bin", "sha256": "c" * 64, "size": 100}],
            )
    assert ei.value.code is ErrorCode.QUOTA_STORAGE_EXCEEDED
    assert ei.value.http_status == 429
    async with pg.engine.connect() as conn:
        file_state = (
            await conn.execute(text("SELECT state FROM task_files WHERE task_id = :t"), {"t": tid})
        ).scalar_one()
        n_res = (
            await conn.execute(
                text("SELECT count(*) FROM task_reservations WHERE task_id = :t"), {"t": tid}
            )
        ).scalar_one()
        status = (
            await conn.execute(text("SELECT status FROM tasks WHERE id = :t"), {"t": tid})
        ).scalar_one()
        user_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM user_quota_usage WHERE user_id = :u"),
                {"u": uid},
            )
        ).scalar_one()
        platform_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM platform_storage WHERE singleton")
            )
        ).scalar_one()
        n_rounds = (
            await conn.execute(
                text("SELECT count(*) FROM task_rounds WHERE task_id = :t"), {"t": tid}
            )
        ).scalar_one()
        n_evs = (
            await conn.execute(
                text("SELECT count(*) FROM task_events WHERE task_id = :t"), {"t": tid}
            )
        ).scalar_one()
    assert file_state == "staged"
    assert n_res == 1  # 仅 active，task_root 未落
    assert status == "uploading"
    assert user_bytes == 0 and platform_bytes == 0
    assert n_rounds == 0 and n_evs == 1  # 仅 create 的 message_saved
