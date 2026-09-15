"""T3 task_service 核心事务测试（Phase 6）。

纪律：owner-RLS 种子走 superuser（tests/v2_task_helpers）；被测服务一律真实
app 角色会话（owner_tx，GUC 已设）执行——禁 superuser 直跑（掩盖 RLS 空转）；
验收①并发创建用双 role_engine 事务 + gather + sorted（顺序无关断言）。
"""

import asyncio
import hashlib
import json
import uuid as _uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.config import get_settings
from backend.errors import AgentCraftError, ErrorCode
from backend.v2.content_hash import canonical_json, content_sha256
from backend.v2.task_service import (
    abort_task,
    commit_input,
    complete_task,
    create_task,
    delete_task,
    get_quota_view,
    get_task_view,
    list_tasks,
    release_task_holdings,
)
from tests.conftest import APP_ROLE
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import (
    owner_tx,
    seed_input_file,
    seed_published_revision,
    seed_running_task,
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
    (uid, pid, revision_id, catalog_id)。"""
    uid = await seed_task_user(pg, "t3-user@x.test")
    pid = await seed_provider(pg, uid)
    rid = await seed_published_revision(pg, uid)
    async with pg.engine.connect() as conn:
        cid = str(
            (
                await conn.execute(
                    text("SELECT catalog_id FROM user_providers WHERE id = :p"),
                    {"p": str(pid)},
                )
            ).scalar_one()
        )
    return uid, pid, rid, cid


def _snapshot(pid, cid) -> dict:
    """D14 快照键（与 ResolvedProvider 一一对应）。"""
    return {
        "provider_id": str(pid),
        "provider_catalog_id": str(cid),
        "provider_model_id": "gpt-4o-mini",
        "provider_key_version": 1,
    }


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


# ---------------------------------------------------------------------------
# create_task：全事务行为与门序
# ---------------------------------------------------------------------------


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
    assert str(task["provider_catalog_id"]) == cid
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


# ---------------------------------------------------------------------------
# complete / abort：直接边与 running 意图位
# ---------------------------------------------------------------------------


async def test_complete_queued_direct_edge(pg, app_engine, domain):
    """契约 C1（Sup §1.2:30 + D19）：queued→completed 直接边——pending round→
    cancelled + completed + active 释放；task_root 与字节账保留 7 天（DB §5:177）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")  # active+task_root+pending 轮
    async with owner_tx(app_engine, uid) as db:
        out = await complete_task(db, owner_id=uid, task_id=tid)
    assert out["task"]["status"] == "completed"
    assert out["task"]["abort_reason"] is None
    assert out["task"]["active_round"] is None
    assert "round" not in out  # 200 形载荷（D14）
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        rnd = (
            await conn.execute(text("SELECT state FROM task_rounds WHERE task_id = :t"), {"t": tid})
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
    assert dict(res) == {"active": "released", "task_root": "held"}  # task_root 保留 7 天
    assert rnd == "cancelled"
    assert [e[0] for e in evs] == ["status_changed", "round_cancelled"]  # 种子 event_sequence=0
    assert [e[1] for e in evs] == [1, 2]
    assert evs[0][2] == {"status": "completed"}
    assert evs[1][2] == {}
    assert usage["active_tasks"] == 0 and usage["running_tasks"] == 0
    assert usage["retained_storage_bytes"] == 0


async def test_complete_running_intent_202(pg, app_engine, domain):
    """D19：running 的 complete → pending_terminal='completed' + 活跃轮→cancelling
    （202 形载荷）；状态/账目不动（轮收口事务裁定）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_running_task(pg, uid, pid)
    async with owner_tx(app_engine, uid) as db:
        out = await complete_task(db, owner_id=uid, task_id=tid)
    assert out["round"] == {"state": "cancelling"}  # 202 形载荷
    assert out["task"]["status"] == "running"
    async with pg.engine.connect() as conn:
        intent = (
            (
                await conn.execute(
                    text("SELECT pending_terminal, status FROM tasks WHERE id = :t"), {"t": tid}
                )
            )
            .mappings()
            .one()
        )
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        rnd = (
            (
                await conn.execute(
                    text("SELECT state, lease_owner FROM task_rounds WHERE task_id = :t"),
                    {"t": tid},
                )
            )
            .mappings()
            .one()
        )
        usage = (
            (
                await conn.execute(
                    text(
                        "SELECT active_tasks, running_tasks FROM user_quota_usage "
                        "WHERE user_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
        slot = (
            (
                await conn.execute(
                    text("SELECT state, task_id FROM platform_slots WHERE slot_no = 1")
                )
            )
            .mappings()
            .one()
        )
        n_evs = (
            await conn.execute(
                text("SELECT count(*) FROM task_events WHERE task_id = :t"), {"t": tid}
            )
        ).scalar_one()
    assert intent["pending_terminal"] == "completed" and intent["status"] == "running"
    assert dict(res) == {"active": "held", "running": "held", "task_root": "held"}
    assert rnd["state"] == "cancelling" and rnd["lease_owner"] == "exec-test"
    assert usage["active_tasks"] == 1 and usage["running_tasks"] == 1
    assert slot["state"] == "leased" and str(slot["task_id"]) == str(tid)
    assert n_evs == 0  # 意图分支不写事件（轮收口时补）


async def test_abort_running_without_round_returns_current_view(pg, app_engine, domain):
    """rowcount=0（无活跃轮=已收口竞态）→ 200 形当前视图不释放、不写意图位。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="running")  # 裸 running（无轮无账）
    async with owner_tx(app_engine, uid) as db:
        out = await abort_task(db, owner_id=uid, task_id=tid)
    assert "round" not in out
    assert out["task"]["status"] == "running"
    async with pg.engine.connect() as conn:
        intent = (
            await conn.execute(text("SELECT pending_terminal FROM tasks WHERE id = :t"), {"t": tid})
        ).scalar_one()
    assert intent is None


async def test_abort_ready_terminalizes_and_releases_active(pg, app_engine, domain):
    """ready→aborted(user_cancel)：active 释放 + active_tasks-1；task_root 保留
    7 天；settled 轮不受影响。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="ready")
    async with pg.engine.begin() as conn:  # 贴合真实生命周期：ready = 轮已 settled
        await conn.execute(
            text("UPDATE task_rounds SET state = 'settled' WHERE task_id = :t"), {"t": tid}
        )
    async with owner_tx(app_engine, uid) as db:
        out = await abort_task(db, owner_id=uid, task_id=tid)
    assert out["task"]["status"] == "aborted"
    assert out["task"]["abort_reason"] == "user_cancel"
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        evs = (
            await conn.execute(
                text("SELECT type, sequence, payload_json FROM task_events WHERE task_id = :t"),
                {"t": tid},
            )
        ).all()
        usage = (
            await conn.execute(
                text("SELECT active_tasks FROM user_quota_usage WHERE user_id = :u"), {"u": uid}
            )
        ).scalar_one()
    assert dict(res) == {"active": "released", "task_root": "held"}
    assert [e[0] for e in evs] == ["status_changed"]
    assert evs[0][2] == {"status": "aborted", "reason": "user_cancel"}
    assert usage == 0


async def test_abort_uploading_rejected_409(pg, app_engine, domain):
    """uploading→aborted 非法迁移（uploading 用 DELETE 终态化）→ TASK_INVALID_
    TRANSITION 409；任务与持有物原样。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await abort_task(db, owner_id=uid, task_id=tid)
    assert ei.value.code is ErrorCode.TASK_INVALID_TRANSITION
    assert ei.value.http_status == 409
    async with pg.engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM tasks WHERE id = :t"), {"t": tid})
        ).scalar_one()
        res = (
            await conn.execute(
                text("SELECT state FROM task_reservations WHERE task_id = :t"), {"t": tid}
            )
        ).scalar_one()
    assert status == "uploading" and res == "held"


# ---------------------------------------------------------------------------
# delete：任意非 deleted → deleted（账面先行全清）
# ---------------------------------------------------------------------------


async def test_delete_running_full_clear(pg, app_engine, domain):
    """running 删除全清：轮 cancelling + pending_terminal='deleted'；三条 reservation
    全 released（RETURNING 闸门退账）；槽位 free；双计数归零；存储账全退。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:  # 预置字节账（task_root 700；双账同额）
        await conn.execute(
            text(
                "UPDATE task_reservations SET bytes = 700 WHERE task_id = :t AND kind = 'task_root'"
            ),
            {"t": tid},
        )
        await conn.execute(
            text("UPDATE user_quota_usage SET retained_storage_bytes = 700 WHERE user_id = :u"),
            {"u": uid},
        )
        await conn.execute(
            text("UPDATE platform_storage SET retained_storage_bytes = 700 WHERE singleton")
        )
    async with owner_tx(app_engine, uid) as db:
        out = await delete_task(db, owner_id=uid, task_id=tid)
    assert out["task"] == {"id": str(tid), "status": "deleted"}  # D14 delete 形状
    async with pg.engine.connect() as conn:
        task = (
            (
                await conn.execute(
                    text("SELECT status, pending_terminal FROM tasks WHERE id = :t"), {"t": tid}
                )
            )
            .mappings()
            .one()
        )
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        rnd = (
            await conn.execute(text("SELECT state FROM task_rounds WHERE task_id = :t"), {"t": tid})
        ).scalar_one()
        slot = (
            (
                await conn.execute(
                    text(
                        "SELECT state, task_id, leased_until FROM platform_slots WHERE slot_no = 1"
                    )
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
        platform_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM platform_storage WHERE singleton")
            )
        ).scalar_one()
        evs = (
            await conn.execute(
                text("SELECT type, sequence FROM task_events WHERE task_id = :t ORDER BY sequence"),
                {"t": tid},
            )
        ).all()
    assert task["status"] == "deleted" and task["pending_terminal"] == "deleted"
    assert dict(res) == {"active": "released", "running": "released", "task_root": "released"}
    assert rnd == "cancelling"  # 活跃轮交轮收口作业收尾
    assert slot["state"] == "free" and slot["task_id"] is None and slot["leased_until"] is None
    assert usage["active_tasks"] == 0 and usage["running_tasks"] == 0
    assert usage["retained_storage_bytes"] == 0 and platform_bytes == 0
    assert [e[0] for e in evs] == ["status_changed"]
    assert evs[0][1] == 1


async def test_delete_terminal_refunds_storage(pg, app_engine, domain):
    """终态（非 deleted）任务删除：task_root/active 全 released + 存储账全退
    （deleted 即时全清 vs 终态 7 天保留的分档收口）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    async with pg.engine.begin() as conn:
        await conn.execute(text("UPDATE tasks SET status = 'completed' WHERE id = :t"), {"t": tid})
        await conn.execute(
            text("UPDATE task_rounds SET state = 'settled' WHERE task_id = :t"), {"t": tid}
        )
        await conn.execute(
            text(
                "UPDATE task_reservations SET bytes = 500 WHERE task_id = :t AND kind = 'task_root'"
            ),
            {"t": tid},
        )
        await conn.execute(
            text("UPDATE user_quota_usage SET retained_storage_bytes = 500 WHERE user_id = :u"),
            {"u": uid},
        )
        await conn.execute(
            text("UPDATE platform_storage SET retained_storage_bytes = 500 WHERE singleton")
        )
    async with owner_tx(app_engine, uid) as db:
        out = await delete_task(db, owner_id=uid, task_id=tid)
    assert out["task"] == {"id": str(tid), "status": "deleted"}
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        usage = (
            (
                await conn.execute(
                    text(
                        "SELECT active_tasks, retained_storage_bytes FROM user_quota_usage "
                        "WHERE user_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
        platform_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM platform_storage WHERE singleton")
            )
        ).scalar_one()
        evs = (
            await conn.execute(
                text("SELECT type, sequence FROM task_events WHERE task_id = :t ORDER BY sequence"),
                {"t": tid},
            )
        ).all()
    assert dict(res) == {"active": "released", "task_root": "released"}
    assert usage["active_tasks"] == 0 and usage["retained_storage_bytes"] == 0
    assert platform_bytes == 0
    assert [e[0] for e in evs] == ["status_changed"]  # settled 轮不补 round_cancelled


async def test_delete_twice_returns_not_found(pg, app_engine, domain):
    """Sup §1.2:31：DELETE 幂等重放由路由层承担；本服务层语义 = 已删除任务统一
    404（新请求）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    async with owner_tx(app_engine, uid) as db:
        out = await delete_task(db, owner_id=uid, task_id=tid)
    assert out["task"]["status"] == "deleted"
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await delete_task(db, owner_id=uid, task_id=tid)
    assert ei.value.code is ErrorCode.TASK_NOT_FOUND
    assert ei.value.http_status == 404


# ---------------------------------------------------------------------------
# 视图与配额面
# ---------------------------------------------------------------------------


async def test_get_task_view_d14_shape_and_excludes_lease(pg, app_engine, domain):
    """D14 视图键集钉死（排除 lease_owner/lease_epoch 红线）；round 摘要仅
    id/state/attempt。"""
    uid, _pid, _rid, _cid = domain
    tid, manifest = await _seed_uploading_with_files(pg, app_engine, domain)
    async with owner_tx(app_engine, uid) as db:
        await commit_input(db, owner_id=uid, task_id=tid, manifest=manifest)
        view = await get_task_view(db, owner_id=uid, task_id=tid)
    assert set(view.keys()) == _D14_VIEW_KEYS
    assert set(view["active_round"].keys()) == {"id", "state", "attempt"}
    assert view["initial_round"] == view["active_round"]
    assert view["counts"] == {"inputs": 2, "outputs": 0}
    assert view["created_at"].endswith("+00:00")
    assert "lease_owner" not in str(view) and "lease_epoch" not in str(view)


async def test_get_task_view_expert_and_provider_display_fields(pg, app_engine, domain):
    """Sup §10.3（Phase 8 T2）：视图键集增 expert{name,avatar_url}/provider
    {display_name,model} 展示字段。display_name 经 provider_catalog join（任务快照
    四键无 display_name），model 直取任务快照；契约键集约束：不加 skills 键
    （skills 经 discover 详情二次拉取，不进任务视图）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    content = {"name": "唤起专家", "avatar_url": "https://cdn.example.com/a.png"}
    async with pg.engine.begin() as conn:  # superuser 回填 content（冻结触发器仅拦 app 上下文）
        rev_id = (
            await conn.execute(
                text("SELECT expert_revision_id FROM tasks WHERE id = CAST(:t AS uuid)"),
                {"t": tid},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "UPDATE expert_revisions SET content_json = CAST(:c AS jsonb), "
                "content_sha256 = :h WHERE id = CAST(:r AS uuid)"
            ),
            {
                "c": json.dumps(content, ensure_ascii=False),
                "h": content_sha256(content),
                "r": str(rev_id),
            },
        )
    async with owner_tx(app_engine, uid) as db:
        view = await get_task_view(db, owner_id=uid, task_id=tid)
    assert view["expert"] == {"name": "唤起专家", "avatar_url": "https://cdn.example.com/a.png"}
    # 0002 种子目录行：api.openai.com → display_name 'OpenAI'；快照 model 直取
    assert view["provider"] == {"display_name": "OpenAI", "model": "m"}
    assert "skills" not in view


async def test_get_task_view_expert_null_after_takedown_and_republish(pg, app_engine, domain):
    """Sup §10.3 null 语义（契约审查 I4）：takedown（实体 status→draft、指针不清，
    §10.6 offline 同语义）后 expert=null 不抛；作者再发布新版（指针前移）后旧
    revision 脱离「实体当前 published 指针」→ 仍 null（历史任务不回溯旧版内容）。
    provider 展示不受内容治理影响。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    async with pg.engine.begin() as conn:  # superuser：0006 guard 仅拦 app 上下文
        await conn.execute(
            text(
                "UPDATE experts SET status = 'draft' WHERE id = "
                "(SELECT expert_id FROM expert_revisions WHERE id = "
                "(SELECT expert_revision_id FROM tasks WHERE id = CAST(:t AS uuid)))"
            ),
            {"t": tid},
        )
    async with owner_tx(app_engine, uid) as db:
        view = await get_task_view(db, owner_id=uid, task_id=tid)
    assert view["expert"] is None
    assert view["provider"] is not None
    # 作者再发布新版：指针前移至 revision_no=2 → 旧 revision 仍不可见
    async with pg.engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT r.expert_id AS expert_id, r.owner_id AS owner_id "
                    "FROM expert_revisions r JOIN tasks t ON t.expert_revision_id = r.id "
                    "WHERE t.id = CAST(:t AS uuid)"
                ),
                {"t": tid},
            )
        ).one()
        new_rev = (
            await conn.execute(
                text(
                    "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) VALUES (gen_random_uuid(), "
                    "CAST(:e AS uuid), CAST(:o AS uuid), 2, CAST(:c AS jsonb), :h, 'published') "
                    "RETURNING id"
                ),
                {
                    "e": str(row.expert_id),
                    "o": str(row.owner_id),
                    "c": json.dumps({"name": "新版专家"}),
                    "h": "b" * 64,
                },
            )
        ).scalar_one()
        await conn.execute(
            text(
                "UPDATE experts SET status = 'published', published_revision_id = "
                "CAST(:r AS uuid) WHERE id = CAST(:e AS uuid)"
            ),
            {"r": str(new_rev), "e": str(row.expert_id)},
        )
    async with owner_tx(app_engine, uid) as db:
        after = await get_task_view(db, owner_id=uid, task_id=tid)
    assert after["expert"] is None
    assert after["provider"] == view["provider"]


async def test_get_task_view_other_owner_404(pg, app_engine, domain):
    """RLS 统一 404：他人任务与缺失任务同形（Sup §7）。"""
    uid, pid, _rid, _cid = domain
    other = await seed_task_user(pg, "t3-other@x.test")
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    with pytest.raises(AgentCraftError) as ei_other:
        async with owner_tx(app_engine, other) as db:
            await get_task_view(db, owner_id=other, task_id=tid)
    assert ei_other.value.code is ErrorCode.TASK_NOT_FOUND
    with pytest.raises(AgentCraftError) as ei_missing:
        async with owner_tx(app_engine, uid) as db:
            await get_task_view(db, owner_id=uid, task_id=str(_uuid.uuid4()))
    assert ei_missing.value.code is ErrorCode.TASK_NOT_FOUND


async def test_task_id_segment_discipline(pg, app_engine, domain):
    """入口纪律：task_id 路径段一律严格 UUID 解析——分隔符/`.`/`..` 等非法串
    400 VALIDATION_ERROR（TaskStorage 路径段永不接收外部裸串）。"""
    uid, _pid, _rid, _cid = domain
    for bad in ("..", "../etc", "a/b", "not-a-uuid"):
        with pytest.raises(HTTPException) as ei:
            async with owner_tx(app_engine, uid) as db:
                await get_task_view(db, owner_id=uid, task_id=bad)
        assert ei.value.status_code == 400
        assert ei.value.detail["code"] == "VALIDATION_ERROR"


async def test_list_tasks_pagination_excludes_deleted(pg, app_engine, domain):
    """分页列表（created_at DESC 稳定序 + total/page/size 信封）；deleted 不可见。"""
    uid, pid, rid, cid = domain
    async with owner_tx(app_engine, uid) as db:
        ids = [
            (await _create_ok(db, uid, pid, rid, cid, message=f"m{i}"))["task"]["id"]
            for i in range(3)
        ]
        page1 = await list_tasks(db, owner_id=uid, page=1, size=2)
        page2 = await list_tasks(db, owner_id=uid, page=2, size=2)
        await delete_task(db, owner_id=uid, task_id=ids[0])
        after_delete = await list_tasks(db, owner_id=uid, page=1, size=10)
    assert page1["total"] == 3 and page1["page"] == 1 and page1["size"] == 2
    assert len(page1["items"]) == 2
    assert page2["total"] == 3 and len(page2["items"]) == 1
    assert page1["items"][0]["id"] != page1["items"][1]["id"]
    assert after_delete["total"] == 2
    assert all(item["id"] != ids[0] for item in after_delete["items"])
    with pytest.raises(HTTPException) as ei:
        async with owner_tx(app_engine, uid) as db:
            await list_tasks(db, owner_id=uid, page=0, size=10)
    assert ei.value.status_code == 400


async def test_get_quota_view_four_dimensions(pg, app_engine, domain):
    """owner 四维用量与上限（daily/active/running/storage）。"""
    uid, pid, _rid, _cid = domain
    await seed_task_for_provider(pg, uid, pid, status="uploading")  # active_tasks=1
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO usage_daily (id, user_id, day, tasks_started) "
                "VALUES (gen_random_uuid(), :u, CURRENT_DATE, 2)"
            ),
            {"u": uid},
        )
        await conn.execute(
            text("UPDATE user_quota_usage SET retained_storage_bytes = 123 WHERE user_id = :u"),
            {"u": uid},
        )
    async with owner_tx(app_engine, uid) as db:
        out = await get_quota_view(db, owner_id=uid)
    assert out["usage"] == {
        "tasks_started_today": 2,
        "active_tasks": 1,
        "running_tasks": 0,
        "retained_storage_bytes": 123,
    }
    assert out["limits"] == {
        "max_daily_tasks": 5,
        "max_active_tasks": 3,
        "max_running_tasks": 1,
        "max_retained_storage_bytes": 1_073_741_824,
    }


# ---------------------------------------------------------------------------
# release_task_holdings：三本账对称释放原语
# ---------------------------------------------------------------------------


async def test_release_holdings_returning_gate_no_double_refund(pg, app_engine, domain):
    """RETURNING 闸门：零行即零退——二次调用不再递减计数、不再退存储（负值防御
    的前置保证）；deleted 分档全清（active+task_root）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    async with pg.engine.begin() as conn:
        await conn.execute(text("UPDATE tasks SET status = 'deleted' WHERE id = :t"), {"t": tid})
        await conn.execute(
            text(
                "UPDATE task_reservations SET bytes = 400 WHERE task_id = :t AND kind = 'task_root'"
            ),
            {"t": tid},
        )
        await conn.execute(
            text("UPDATE user_quota_usage SET retained_storage_bytes = 400 WHERE user_id = :u"),
            {"u": uid},
        )
        await conn.execute(
            text("UPDATE platform_storage SET retained_storage_bytes = 400 WHERE singleton")
        )
    async with owner_tx(app_engine, uid) as db:
        await release_task_holdings(db, task_id=tid, owner_id=uid)
        await release_task_holdings(db, task_id=tid, owner_id=uid)  # 零行：零退
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        usage = (
            (
                await conn.execute(
                    text(
                        "SELECT active_tasks, retained_storage_bytes FROM user_quota_usage "
                        "WHERE user_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
        platform_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM platform_storage WHERE singleton")
            )
        ).scalar_one()
    assert dict(res) == {"active": "released", "task_root": "released"}
    assert usage["active_tasks"] == 0 and usage["retained_storage_bytes"] == 0
    assert platform_bytes == 0


async def test_release_holdings_non_terminal_keeps_active(pg, app_engine, domain):
    """非终态（settle→ready 调用形态）：只回收轮账；active/task_root 不受影响、
    active_tasks 不减。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="ready")  # active+task_root held
    async with owner_tx(app_engine, uid) as db:
        await release_task_holdings(db, task_id=tid, owner_id=uid)
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        usage = (
            await conn.execute(
                text("SELECT active_tasks FROM user_quota_usage WHERE user_id = :u"), {"u": uid}
            )
        ).scalar_one()
    assert dict(res) == {"active": "held", "task_root": "held"}
    assert usage == 1


async def test_release_holdings_missing_task_silent(pg, app_engine):
    """任务行不存在（D18 物理删竞态）→ 静默返回（D16 弃权语义），不抛。"""
    uid = await seed_task_user(pg, "t3-silent@x.test")
    async with owner_tx(app_engine, uid) as db:
        await release_task_holdings(db, task_id=str(_uuid.uuid4()), owner_id=uid)
