"""T5 dispatcher 与四后台作业测试（Phase 6，D16 两段式会话编排）。

纪律：owner-RLS 种子走 superuser（tests/v2_task_helpers / v2_provider_helpers）；
被测 dispatcher 函数一律经 make_v2_runtime 真实 app/admin 双 role 执行——禁
superuser 直跑（D16 的意义就是防「测试全绿生产空转」）；并发用例 gather + sorted
顺序无关断言；时钟用 DB 侧回拨构造（test_v2_outbox.py:161-187 先例）；abort 联动
经真实 owner_session + T3 abort_task（V1-only 冒烟钉除外全部双 role）。
"""

import asyncio
import logging
from contextlib import suppress

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.database import get_db
from backend.main import app
from backend.v2 import task_dispatcher as td_mod
from backend.v2.runtime import owner_session, v2_runtime_from_settings
from backend.v2.task_dispatcher import (
    _instance_id,
    dispatch_once,
    dispatcher_loop,
    reclaim_once,
    sweep_terminal_cleanup,
    sweep_upload_ttl,
)
from backend.v2.task_service import abort_task
from backend.v2.task_storage import TaskStorage
from tests.conftest import PgDb
from tests.test_v2_runtime import make_v2_runtime
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import seed_running_task, seed_task_user

_DISPATCH_LOGGER = "agentcraft.task.dispatcher"


@pytest.fixture
async def rt(pg: PgDb):
    """app/admin 双 role 运行时（RLS 真实生效）；executor 句柄由用例按需注入。"""
    runtime = make_v2_runtime(pg)
    yield runtime
    runtime.close()


class StubExecutor:
    """T6a 前 executor 替身：notify 记录 + container_alive 可控（领取/账目在 DB 断言）。"""

    def __init__(self) -> None:
        self.notified: list[str] = []
        self.alive = True
        self.fail_notify = False

    async def notify(self, task_id: str) -> None:
        if self.fail_notify:
            raise RuntimeError("notify down")
        self.notified.append(task_id)

    async def container_alive(self, task_id: str) -> bool:
        return self.alive


# ---------- 播种/断言助手（superuser 只读断言 + 种子，不跑被测服务） ----------


async def _seed_queued_with_round(pg: PgDb, uid: str, pid: str) -> tuple[str, str]:
    """queued 任务全套（active+task_root 账、初始消息、pending 轮）→ (task_id, round_id)。"""
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    async with pg.engine.connect() as conn:
        rid = (
            await conn.execute(text("SELECT id FROM task_rounds WHERE task_id = :t"), {"t": tid})
        ).scalar_one()
    return str(tid), str(rid)


async def _backdate_task(pg: PgDb, task_id: str, interval_sql: str) -> None:
    """DB 时钟回拨 tasks.created_at（uploading TTL / 7 天保留期边界构造）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE tasks SET created_at = now() - interval '{interval_sql}' WHERE id = :t"),
            {"t": task_id},
        )


async def _scalar(pg: PgDb, sql: str, **params):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one_or_none()


async def _row_map(pg: PgDb, sql: str, **params):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).mappings().one()


async def _rows(pg: PgDb, sql: str, **params):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).all()


async def _expire_round_lease(pg: PgDb, round_id: str) -> None:
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET lease_expires_at = now() - interval '1 second' "
                "WHERE id = :r"
            ),
            {"r": round_id},
        )


# ---------- dispatch_once：领槽（D16 两段式） ----------


async def test_dispatch_claims_round_full_state(pg, rt):
    uid = await seed_task_user(pg, "d1@x.test")
    pid = await seed_provider(pg, uid)
    tid, rid = await _seed_queued_with_round(pg, uid, pid)
    rt.executor = StubExecutor()

    assert await dispatch_once(rt) == 1
    assert rt.executor.notified == [tid]

    rrow = await _row_map(
        pg,
        "SELECT state, lease_owner, lease_epoch, attempt, "
        "extract(epoch FROM (lease_expires_at - now())) AS ttl "
        "FROM task_rounds WHERE id = :r",
        r=rid,
    )
    assert rrow["state"] == "running"
    assert rrow["lease_owner"] == _instance_id()
    assert rrow["lease_epoch"] == 1 and rrow["attempt"] == 1  # 种子 0 → 各 +1
    assert 80 <= rrow["ttl"] <= 90  # lease_ttl_seconds=90（DB 时钟）

    trow = await _row_map(pg, "SELECT status, event_sequence FROM tasks WHERE id = :t", t=tid)
    assert trow["status"] == "running" and trow["event_sequence"] == 2

    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = :t AND kind = 'running'",
            t=tid,
        )
        == "held"
    )
    assert (
        await _scalar(pg, "SELECT running_tasks FROM user_quota_usage WHERE user_id = :u", u=uid)
        == 1
    )
    slot = await _row_map(
        pg, "SELECT state, task_id::text AS tid FROM platform_slots WHERE slot_no = 1"
    )
    assert slot["state"] == "leased" and slot["tid"] == tid

    events = await _rows(
        pg,
        "SELECT sequence, type, payload_json FROM task_events WHERE task_id = :t ORDER BY sequence",
        t=tid,
    )
    assert [(e.sequence, e.type) for e in events] == [(1, "status_changed"), (2, "round_running")]
    assert events[0].payload_json == {"status": "running"}
    assert events[1].payload_json == {"round_id": rid, "attempt": 1}

    assert await dispatch_once(rt) == 0  # 无剩余 queued 任务


async def test_dispatch_without_executor_returns_zero(pg, rt):
    """T6a 前 runtime 无 executor 句柄 → 直接 0，不圈定不领取（防空转 churn）。"""
    uid = await seed_task_user(pg, "d2@x.test")
    pid = await seed_provider(pg, uid)
    tid, rid = await _seed_queued_with_round(pg, uid, pid)

    assert await dispatch_once(rt) == 0
    rrow = await _row_map(
        pg, "SELECT state, attempt, lease_owner FROM task_rounds WHERE id = :r", r=rid
    )
    assert rrow["state"] == "pending" and rrow["attempt"] == 0 and rrow["lease_owner"] is None
    assert await _scalar(pg, "SELECT count(*) FROM task_events WHERE task_id = :t", t=tid) == 0
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "queued"


async def test_dispatch_no_free_slot_drops_candidate_wholesale(pg, rt):
    uid = await seed_task_user(pg, "d3@x.test")
    pid = await seed_provider(pg, uid)
    tid, rid = await _seed_queued_with_round(pg, uid, pid)
    rt.executor = StubExecutor()
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE platform_slots SET state = 'leased', "
                "leased_until = now() + interval '1 hour' WHERE state = 'free'"
            )
        )

    assert await dispatch_once(rt) == 0  # 无空闲槽：候选整体回滚
    rrow = await _row_map(
        pg, "SELECT state, attempt, lease_owner FROM task_rounds WHERE id = :r", r=rid
    )
    assert rrow["state"] == "pending" and rrow["attempt"] == 0 and rrow["lease_owner"] is None
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "queued"
    assert (
        await _scalar(pg, "SELECT count(*) FROM task_reservations WHERE task_id = :t", t=tid)
        == 2  # 仅种子的 active+task_root，running 未插
    )
    assert (
        await _scalar(pg, "SELECT running_tasks FROM user_quota_usage WHERE user_id = :u", u=uid)
        == 0
    )
    assert await _scalar(pg, "SELECT count(*) FROM task_events WHERE task_id = :t", t=tid) == 0

    # 恢复一槽：下一轮重试可达
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE platform_slots SET state = 'free', leased_until = NULL WHERE slot_no = 1")
        )
    assert await dispatch_once(rt) == 1


async def test_dispatch_running_quota_blocks_second_round(pg, rt):
    uid = await seed_task_user(pg, "d4@x.test")  # max_running_tasks=1（平台默认）
    pid = await seed_provider(pg, uid)
    t1, r1 = await _seed_queued_with_round(pg, uid, pid)
    t2, _r2 = await _seed_queued_with_round(pg, uid, pid)
    async with pg.engine.begin() as conn:  # 钉 FIFO：r1 确定性地排在前
        await conn.execute(
            text("UPDATE task_rounds SET created_at = now() - interval '1 second' WHERE id = :r"),
            {"r": r1},
        )
    rt.executor = StubExecutor()

    assert await dispatch_once(rt) == 1  # 首轮领取，次轮配额仲裁弃
    assert rt.executor.notified == [t1]
    assert await dispatch_once(rt) == 0  # 次轮仍在圈定面、每轮重试每轮弃
    row = await _row_map(
        pg,
        "SELECT state, attempt FROM task_rounds WHERE id = ("
        "SELECT id FROM task_rounds WHERE task_id = :t)",
        t=t2,
    )
    assert row["state"] == "pending" and row["attempt"] == 0
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=t2) == "queued"


async def test_concurrent_dispatch_same_round_single_winner(pg):
    """两并发 dispatch_once 同轮恰一胜（D16 条件仲裁；顺序无关断言）。"""
    uid = await seed_task_user(pg, "d5@x.test")
    pid = await seed_provider(pg, uid)
    tid, rid = await _seed_queued_with_round(pg, uid, pid)
    rt1, rt2 = make_v2_runtime(pg), make_v2_runtime(pg)  # 模拟双实例独立连接池
    rt1.executor = StubExecutor()
    rt2.executor = StubExecutor()
    try:
        results = await asyncio.gather(dispatch_once(rt1), dispatch_once(rt2))
    finally:
        rt1.close()
        rt2.close()

    assert sorted(results) == [0, 1]
    assert len(rt1.executor.notified) + len(rt2.executor.notified) == 1  # 仅胜者 notify
    rrow = await _row_map(
        pg, "SELECT state, lease_epoch, attempt FROM task_rounds WHERE id = :r", r=rid
    )
    assert rrow["state"] == "running" and rrow["lease_epoch"] == 1 and rrow["attempt"] == 1
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_reservations WHERE task_id = :t "
            "AND kind = 'running' AND state = 'held'",
            t=tid,
        )
        == 1
    )
    assert await _scalar(pg, "SELECT count(*) FROM platform_slots WHERE state = 'leased'") == 1
    assert (
        await _scalar(pg, "SELECT running_tasks FROM user_quota_usage WHERE user_id = :u", u=uid)
        == 1
    )
    assert await _scalar(pg, "SELECT count(*) FROM task_events WHERE task_id = :t", t=tid) == 2


async def test_notify_failure_does_not_lose_claim(pg, rt, caplog):
    uid = await seed_task_user(pg, "d6@x.test")
    pid = await seed_provider(pg, uid)
    tid, _rid = await _seed_queued_with_round(pg, uid, pid)
    rt.executor = StubExecutor()
    rt.executor.fail_notify = True

    assert await dispatch_once(rt) == 1  # 领取成立，notify 失败仅记录
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "running"
    assert any("executor notify failed" in r.getMessage() for r in caplog.records)


# ---------- reclaim_once：过期 lease 回收 / attempt 档位 / cancelling 收口 ----------


async def test_reclaim_expired_lease_requeues(pg, rt):
    uid = await seed_task_user(pg, "d8@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    rid = str(await _scalar(pg, "SELECT id FROM task_rounds WHERE task_id = :t", t=tid))
    await _expire_round_lease(pg, rid)

    assert await reclaim_once(rt) == 1  # 崩溃恢复：executor 缺位不碍过期回收
    rrow = await _row_map(
        pg,
        "SELECT state, lease_owner, lease_expires_at, lease_epoch, attempt "
        "FROM task_rounds WHERE id = :r",
        r=rid,
    )
    assert rrow["state"] == "pending"
    assert rrow["lease_owner"] is None and rrow["lease_expires_at"] is None
    assert rrow["attempt"] == 1  # attempt 不动——重领时 claim 再 +1
    assert rrow["lease_epoch"] == 1  # epoch 单调保留（清两元组不清计数器）
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "queued"
    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = :t AND kind = 'running'",
            t=tid,
        )
        == "released"
    )
    assert await _scalar(pg, "SELECT state FROM platform_slots WHERE slot_no = 1") == "free"
    assert (
        await _scalar(pg, "SELECT running_tasks FROM user_quota_usage WHERE user_id = :u", u=uid)
        == 0
    )
    events = await _rows(
        pg, "SELECT type FROM task_events WHERE task_id = :t ORDER BY sequence", t=tid
    )
    assert [e.type for e in events] == ["status_changed", "round_queued"]

    assert await reclaim_once(rt) == 0  # 围栏后不可重复回收

    rt.executor = StubExecutor()
    assert await dispatch_once(rt) == 1  # 重领可达：epoch/attempt 单调推进
    rrow = await _row_map(
        pg, "SELECT state, lease_epoch, attempt FROM task_rounds WHERE id = :r", r=rid
    )
    assert rrow["state"] == "running" and rrow["lease_epoch"] == 2 and rrow["attempt"] == 2


async def test_concurrent_reclaim_expired_lease_single_winner(pg):
    uid = await seed_task_user(pg, "d9@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    rid = str(await _scalar(pg, "SELECT id FROM task_rounds WHERE task_id = :t", t=tid))
    await _expire_round_lease(pg, rid)

    rt1, rt2 = make_v2_runtime(pg), make_v2_runtime(pg)
    try:
        results = await asyncio.gather(reclaim_once(rt1), reclaim_once(rt2))
    finally:
        rt1.close()
        rt2.close()

    assert sorted(results) == [0, 1]  # 写侧围栏：并发回收恰一胜
    rrow = await _row_map(
        pg, "SELECT state, lease_owner, attempt FROM task_rounds WHERE id = :r", r=rid
    )
    assert rrow["state"] == "pending" and rrow["lease_owner"] is None
    assert rrow["attempt"] == 1
    assert (
        await _scalar(pg, "SELECT running_tasks FROM user_quota_usage WHERE user_id = :u", u=uid)
        == 0
    )


async def test_reclaim_attempt_limit_round_fails(pg, rt):
    uid = await seed_task_user(pg, "d10@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET attempt = 3, "
                "lease_expires_at = now() - interval '1 second' WHERE task_id = :t"
            ),
            {"t": tid},
        )

    assert await reclaim_once(rt) == 1
    rrow = await _row_map(
        pg, "SELECT state, attempt, lease_owner FROM task_rounds WHERE task_id = :t", t=tid
    )
    assert rrow["state"] == "failed" and rrow["attempt"] == 3 and rrow["lease_owner"] is None
    trow = await _row_map(pg, "SELECT status, abort_reason FROM tasks WHERE id = :t", t=tid)
    assert trow["status"] == "failed" and trow["abort_reason"] == "round_failed"
    assert (
        await _scalar(pg, "SELECT running_tasks FROM user_quota_usage WHERE user_id = :u", u=uid)
        == 0
    )
    assert (
        await _scalar(pg, "SELECT active_tasks FROM user_quota_usage WHERE user_id = :u", u=uid)
        == 0
    )  # 终态 failed：running + active 双释放（task_root 保留 7 天）
    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = :t "
            "AND kind = 'task_root' AND state = 'held'",
            t=tid,
        )
        == "held"
    )
    assert await _scalar(pg, "SELECT state FROM platform_slots WHERE slot_no = 1") == "free"
    events = await _rows(
        pg,
        "SELECT type, payload_json FROM task_events WHERE task_id = :t ORDER BY sequence",
        t=tid,
    )
    assert [e.type for e in events] == ["status_changed", "round_failed"]
    assert events[0].payload_json == {"status": "failed", "reason": "round_failed"}


async def test_reclaim_cancelling_round_container_gated(pg, rt):
    uid = await seed_task_user(pg, "d11@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    async with owner_session(rt, uid) as db:  # 真实 abort 链路写 cancelling 意图
        await abort_task(db, owner_id=uid, task_id=tid)

    assert await reclaim_once(rt) == 0  # 无 executor：cancelling 不可确认，跳过
    assert (
        await _scalar(pg, "SELECT state FROM task_rounds WHERE task_id = :t", t=tid) == "cancelling"
    )

    rt.executor = StubExecutor()  # alive=True：容器仍在，不与 settle 竞写
    assert await reclaim_once(rt) == 0
    assert (
        await _scalar(pg, "SELECT state FROM task_rounds WHERE task_id = :t", t=tid) == "cancelling"
    )

    rt.executor.alive = False
    assert await reclaim_once(rt) == 1  # 容器确认已死 → 幂等 cancelled + 轮账释放
    assert (
        await _scalar(pg, "SELECT state FROM task_rounds WHERE task_id = :t", t=tid) == "cancelled"
    )
    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = :t AND kind = 'running'",
            t=tid,
        )
        == "released"
    )
    assert await _scalar(pg, "SELECT state FROM platform_slots WHERE slot_no = 1") == "free"
    assert (
        await _scalar(pg, "SELECT running_tasks FROM user_quota_usage WHERE user_id = :u", u=uid)
        == 0
    )
    # 任务保持 running + pending_terminal：终态化属 T6a settle 面（本作业只对轮负责）
    trow = await _row_map(pg, "SELECT status, pending_terminal FROM tasks WHERE id = :t", t=tid)
    assert trow["status"] == "running" and trow["pending_terminal"] == "aborted"
    events = await _rows(pg, "SELECT type FROM task_events WHERE task_id = :t", t=tid)
    assert [e.type for e in events] == ["round_cancelled"]

    assert await reclaim_once(rt) == 0  # 幂等：已 cancelled 不再圈定


# ---------- sweep_upload_ttl / sweep_terminal_cleanup ----------


async def test_upload_ttl_25h_swept_23h_not(pg, rt):
    uid = await seed_task_user(pg, "d12@x.test")
    pid = await seed_provider(pg, uid)
    expired = str(await seed_task_for_provider(pg, uid, pid, status="uploading"))
    fresh = str(await seed_task_for_provider(pg, uid, pid, status="uploading"))
    await _backdate_task(pg, expired, "25 hours")
    async with pg.engine.begin() as conn:  # 23h：边界内不扫
        await conn.execute(
            text("UPDATE tasks SET created_at = now() - interval '23 hours' WHERE id = :t"),
            {"t": fresh},
        )

    assert await sweep_upload_ttl(rt) == 1
    row = await _row_map(pg, "SELECT status, abort_reason FROM tasks WHERE id = :t", t=expired)
    assert row["status"] == "failed" and row["abort_reason"] == "upload_expired"
    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = :t AND kind = 'active'",
            t=expired,
        )
        == "released"
    )
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=fresh) == "uploading"
    assert (
        await _scalar(pg, "SELECT active_tasks FROM user_quota_usage WHERE user_id = :u", u=uid)
        == 1  # fresh 仍占 1；expired 已随释放递减
    )
    events = await _rows(
        pg, "SELECT type, payload_json FROM task_events WHERE task_id = :t", t=expired
    )
    assert events[0].type == "status_changed"
    assert events[0].payload_json == {"status": "failed", "reason": "upload_expired"}

    assert await sweep_upload_ttl(rt) == 0  # 幂等：无第二遍


async def test_terminal_cleanup_8d_cleaned_6d_not_and_returning_gate(pg, rt, tmp_path):
    rt.storage = TaskStorage(tmp_path / "task-storage")
    uid = await seed_task_user(pg, "d13@x.test")
    pid = await seed_provider(pg, uid)
    old = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    young = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    async with pg.engine.begin() as conn:
        for tid, back in ((old, "8 days"), (young, "6 days")):
            await conn.execute(
                text(
                    f"UPDATE tasks SET status = 'completed', "
                    f"created_at = now() - interval '{back}' WHERE id = :t"
                ),
                {"t": tid},
            )
            await conn.execute(
                text(
                    "UPDATE task_reservations SET bytes = 1000 "
                    "WHERE task_id = :t AND kind = 'task_root'"
                ),
                {"t": tid},
            )
        await conn.execute(
            text("UPDATE user_quota_usage SET retained_storage_bytes = 2000 WHERE user_id = :u"),
            {"u": uid},
        )
        await conn.execute(text("UPDATE platform_storage SET retained_storage_bytes = 2000"))

    f1 = rt.storage.input_path(old, "f1")
    f1.parent.mkdir(parents=True, exist_ok=True)
    f1.write_text("x")
    f2 = rt.storage.artifact_path(old, "f2")
    f2.write_text("y")

    assert await sweep_terminal_cleanup(rt) == 1
    assert await _scalar(pg, "SELECT count(*) FROM task_events WHERE task_id = :t", t=old) == 0
    assert await _scalar(pg, "SELECT count(*) FROM task_rounds WHERE task_id = :t", t=old) == 0
    assert await _scalar(pg, "SELECT count(*) FROM task_messages WHERE task_id = :t", t=old) == 0
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_reservations WHERE task_id = :t AND state = 'held'",
            t=old,
        )
        == 0
    )
    assert (
        await _scalar(
            pg,
            "SELECT retained_storage_bytes FROM user_quota_usage WHERE user_id = :u",
            u=uid,
        )
        == 1000  # 仅 old 的 1000 退账；young 的 task_root 未动
    )
    assert await _scalar(pg, "SELECT retained_storage_bytes FROM platform_storage") == 1000
    assert not f1.exists() and not f2.exists()  # post-commit 物理删
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=young) == "completed"

    # RETURNING 闸门：重扫零动作零二次退（入参即为幂等证明）
    assert await sweep_terminal_cleanup(rt) == 0
    assert (
        await _scalar(
            pg,
            "SELECT retained_storage_bytes FROM user_quota_usage WHERE user_id = :u",
            u=uid,
        )
        == 1000
    )
    assert await _scalar(pg, "SELECT retained_storage_bytes FROM platform_storage") == 1000


async def test_terminal_cleanup_removes_extension_script(pg, rt, tmp_path):
    """终审 Important #1：终态保留期 sweep 连带删同任务扩展脚本
    extensions/task-<id>.ts（delete_task_storage 范围不变，清理点负责）；
    他人扩展脚本不受影响；幂等重扫不复活。"""
    rt.storage = TaskStorage(tmp_path / "task-storage")
    uid = await seed_task_user(pg, "d13b@x.test")
    pid = await seed_provider(pg, uid)
    old = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    other = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE tasks SET status = 'completed', "
                "created_at = now() - interval '8 days' WHERE id = :t"
            ),
            {"t": old},
        )

    gone = rt.storage.extension_path(old)
    gone.write_text("old-ext", encoding="utf-8")
    keep = rt.storage.extension_path(other)
    keep.write_text("keep-ext", encoding="utf-8")
    assert gone.exists() and keep.exists()

    assert await sweep_terminal_cleanup(rt) == 1
    assert not gone.exists()  # 同任务扩展脚本随 sweep 物理删
    assert keep.exists()  # 他人扩展脚本不在圈定面

    assert await sweep_terminal_cleanup(rt) == 0  # 幂等：重扫零动作


# ---------- dispatcher_loop 生命周期 ----------


async def test_dispatcher_loop_survives_cycle_error_and_cancels(pg, rt, monkeypatch, caplog):
    calls = {"n": 0}
    reclaim_calls = {"n": 0}

    async def flaky_dispatch(_rt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip")
        return 0

    async def spy_reclaim(_rt):
        reclaim_calls["n"] += 1
        return 0

    monkeypatch.setattr(td_mod, "dispatch_once", flaky_dispatch)
    monkeypatch.setattr(td_mod, "reclaim_once", spy_reclaim)
    caplog.set_level(logging.ERROR, logger=_DISPATCH_LOGGER)
    task = asyncio.create_task(dispatcher_loop(rt, poll_seconds=0.01))
    try:
        async with asyncio.timeout(5):
            while calls["n"] < 2 or reclaim_calls["n"] < 2:  # 首轮异常后循环继续
                await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert task.done() and task.cancelled()
    assert reclaim_calls["n"] >= 2  # 四作业串行：异常轮后 reclaim 仍逐轮执行
    assert any("task dispatcher cycle failed" in r.getMessage() for r in caplog.records)


# ---------- V1-only 冒烟钉 ----------


def test_v1_only_startup_starts_no_dispatcher(test_db, caplog):
    """双 DSN 空环境（conftest neutralize）下 TestClient 启动：V1-only 行为完全不变——
    不创建任务 dispatcher 协程、启动日志零 dispatcher 异常。"""
    captured: list[str] = []

    async def override_get_db():
        async with test_db.session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as http:
            assert http.get("/api/health").status_code == 200
    finally:
        app.dependency_overrides.pop(get_db, None)
        captured.append("done")

    assert v2_runtime_from_settings() is None  # V1-only：dispatcher 根本不创建
    dispatcher_logs = [
        r
        for r in caplog.records
        if r.name == _DISPATCH_LOGGER or "task dispatcher" in r.getMessage()
    ]
    assert not dispatcher_logs  # 无 dispatcher 协程残留 / 启动异常
