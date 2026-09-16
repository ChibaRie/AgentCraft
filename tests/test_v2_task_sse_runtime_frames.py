"""实时保真补点位（Phase 8 T5，§9.10.10 义务）：各系统写点 post-commit 推帧。

帧形状约定：status_changed/queued 持久帧携 event_sequence 水位键（吃合并去重与
id 行，message_saved 泵先例同语义）；done 瞬态帧与执行器泵同形（无水位键）。
Phase 9 T6 自 test_v2_task_sse.py 后半逐字搬移；共享助手见 v2_sse_helpers。
"""

import asyncio
import threading

import pytest
from sqlalchemy import text

from backend.v2 import task_executor as te
from backend.v2 import task_service
from backend.v2.runtime import owner_session
from backend.v2.task_dispatcher import _instance_id, dispatch_once, reclaim_once, sweep_upload_ttl
from backend.v2.task_executor import (
    build_terminator,
    reconcile_pending_terminal,
)
from backend.v2.task_streams import TaskStreamRegistry
from tests import v2_sse_helpers as _sse_helpers
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_sse_helpers import (
    _frames,
    _make_ready,
    _next_frame,
    _open_stream,
    _rows,
    _StreamsHost,
    _wire_executor,
)
from tests.v2_task_helpers import api_env as api_env  # noqa: F401  # re-export 夹具
from tests.v2_task_helpers import (
    login_client as _login,
)
from tests.v2_task_helpers import (
    seed_login_domain as _seed_domain,
)
from tests.v2_task_helpers import (
    seed_running_task,
    seed_task_user,
)

# stream_env 夹具发现形态：以赋值别名引入（admin_env 同款惯例）。
stream_env = _sse_helpers.stream_env

# ---------------------------------------------------------------------------
# 实时保真补点位（Phase 8 T5，§9.10.10 义务）：各系统写点 post-commit 推帧。
# 帧形状约定：status_changed/queued 持久帧携 event_sequence 水位键（吃合并去重
# 与 id 行，message_saved 泵先例同语义）；done 瞬态帧与执行器泵同形（无水位键）。
# ---------------------------------------------------------------------------


async def test_send_message_publishes_three_frames_after_commit(pg, stream_env):
    """①ready 任务 send_message：message_saved + status_changed(queued) +
    round_queued(queued) 三帧在 202 事务提交后实时到达（帧序=事件序）。"""
    uid, pid, _ = await _seed_domain(pg, "t8b-pub-send@example.com")
    client = await _login(pg, "t8b-pub-send@example.com")
    tid = await _make_ready(pg, uid, pid)

    resp = await _open_stream(stream_env, uid, tid, after=1)
    try:
        meta = await _next_frame(resp)
        assert meta["data"] == {"task_id": tid, "status": "ready", "event_sequence": 1}

        sent = await client.post(
            f"/api/tasks/{tid}/messages",
            json={"content": "实时三帧"},
            headers={"Idempotency-Key": "t8b-pub-send-1"},
        )
        assert sent.status_code == 202, sent.text
        data = sent.json()["data"]

        saved, flipped, queued = await _frames(resp, 3)
        assert (saved["id"], saved["event"]) == (2, "message_saved")
        assert saved["data"] == {
            "message_id": data["message"]["id"],
            "event_sequence": 2,
            "author": "user",
        }
        assert (flipped["id"], flipped["event"]) == (3, "status_changed")
        assert flipped["data"] == {
            "status": "queued",
            "abort_reason": None,
            "event_sequence": 3,
        }
        assert (queued["id"], queued["event"]) == (4, "queued")
        assert queued["data"] == {"round_id": data["round_id"], "event_sequence": 4}
    finally:
        await resp.body_iterator.aclose()

    # 对账：帧序=落库事件序（post-commit 纪律——提交先于推帧）
    events = await _rows(
        pg,
        "SELECT sequence, type FROM task_events WHERE task_id = :t ORDER BY sequence",
        {"t": tid},
    )
    assert [(e.sequence, e.type) for e in events] == [
        (2, "message_saved"),
        (3, "status_changed"),
        (4, "round_queued"),
    ]


async def test_dispatch_claim_publishes_status_running_live(pg, api_env):
    """②queued 任务 dispatcher 领槽：status_changed(running) 帧实时到达（不待
    轮事件——run_pending 未调，round_running 无独立帧亦无执行链帧）。"""
    uid, pid, _ = await _seed_domain(pg, "t8b-pub-dispatch@example.com")
    client = await _login(pg, "t8b-pub-dispatch@example.com")
    tid = await _make_ready(pg, uid, pid)
    async with pg.engine.begin() as conn:  # running 配额行（API 创建路径外的种子面）
        await conn.execute(
            text(
                "INSERT INTO user_quotas (user_id, max_daily_tasks, max_active_tasks, "
                "max_running_tasks, max_retained_storage_bytes) VALUES (:u, 5, 3, 1, "
                "1073741824) ON CONFLICT (user_id) DO NOTHING"
            ),
            {"u": uid},
        )
    sent = await client.post(
        f"/api/tasks/{tid}/messages",
        json={"content": "排队领槽"},
        headers={"Idempotency-Key": "t8b-pub-dispatch-1"},
    )
    assert sent.status_code == 202, sent.text

    _wire_executor(api_env)  # 领取面 notify 真件；不 run_pending（无轮执行）
    resp = await _open_stream(api_env, uid, tid, after=4)
    try:
        meta = await _next_frame(resp)
        assert meta["data"] == {"task_id": tid, "status": "queued", "event_sequence": 4}

        assert await dispatch_once(api_env) == 1
        running = await _next_frame(resp)
        assert (running["id"], running["event"]) == (5, "status_changed")
        assert running["data"] == {
            "status": "running",
            "abort_reason": None,
            "event_sequence": 5,
        }

        # 不待轮事件：此后再无帧（round_running@6 无独立帧；执行链未启动）
        with pytest.raises(asyncio.TimeoutError):
            await _next_frame(resp, timeout=1.0)
    finally:
        await resp.body_iterator.aclose()


class _DeadContainerHost:
    """reclaim cancelling 收口替身：container_alive 恒 False（容器确认已死）+
    注册表在位（publish 消费面）。"""

    def __init__(self) -> None:
        self.streams = TaskStreamRegistry()

    async def container_alive(self, task_id: str) -> bool:
        return False


async def test_reclaim_cancelling_publishes_done_aborted_frame(pg, api_env):
    """③reclaim（cancelling 分支）：容器确认已死收口 round_cancelled →
    done(aborted) 帧实时到达（孤写无伴随 status_changed——§9.10.8）。"""
    uid = await seed_task_user(pg, "t8b-pub-reclaim@example.com")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:  # 种子水位对齐（message@1）
        await conn.execute(text("UPDATE tasks SET event_sequence = 1 WHERE id = :t"), {"t": tid})
    async with owner_session(api_env, uid) as db:  # 真实 abort 链路写 cancelling 意图
        await task_service.abort_task(db, owner_id=uid, task_id=tid)

    api_env.executor = _DeadContainerHost()
    resp = await _open_stream(api_env, uid, tid, after=1)
    try:
        meta = await _next_frame(resp)
        assert meta["data"] == {"task_id": tid, "status": "running", "event_sequence": 1}

        assert await reclaim_once(api_env) == 1
        done = await _next_frame(resp)
        assert done["id"] is None  # done 瞬态帧无 id 行
        assert done["event"] == "done"
        assert done["data"] == {"finish_reason": "aborted", "usage": {}}
    finally:
        await resp.body_iterator.aclose()


async def test_reclaim_expired_fail_publishes_failed_frames(pg, api_env):
    """③reclaim fence（attempt>=3 档）：围栏翻转 → status_changed(failed) 带序 +
    round_failed → done(error) 帧，提交后按事件序实时到达。"""
    uid = await seed_task_user(pg, "t8b-pub-reclaim2@example.com")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:
        await conn.execute(text("UPDATE tasks SET event_sequence = 1 WHERE id = :t"), {"t": tid})
        await conn.execute(
            text(
                "UPDATE task_rounds SET attempt = 3, "
                "lease_expires_at = now() - interval '1 second' WHERE task_id = :t"
            ),
            {"t": tid},
        )
    api_env.executor = _StreamsHost()  # 过期回收不触 executor 方法；注册表在位
    resp = await _open_stream(api_env, uid, tid, after=1)
    try:
        meta = await _next_frame(resp)
        assert meta["data"]["status"] == "running"

        assert await reclaim_once(api_env) == 1
        failed, done = await _frames(resp, 2)
        assert (failed["id"], failed["event"]) == (2, "status_changed")
        assert failed["data"] == {
            "status": "failed",
            "abort_reason": "round_failed",
            "event_sequence": 2,
        }
        assert done["id"] is None
        assert done["data"] == {"finish_reason": "error", "usage": {}}
    finally:
        await resp.body_iterator.aclose()


async def test_reclaim_expired_requeue_publishes_queued_frames(pg, api_env):
    """③reclaim fence（attempt<3 档 / T5 遗留补测）：围栏翻转 → 轮回 pending +
    任务 running→queued → status_changed(queued) 带序 + round_queued → queued 帧，
    提交后按事件序实时到达；与 failed 档共用同一 reclaim 入口但帧组不同。"""
    uid = await seed_task_user(pg, "t8b-pub-requeue@example.com")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:
        await conn.execute(text("UPDATE tasks SET event_sequence = 1 WHERE id = :t"), {"t": tid})
        await conn.execute(
            text(
                "UPDATE task_rounds SET attempt = 1, "
                "lease_expires_at = now() - interval '1 second' WHERE task_id = :t"
            ),
            {"t": tid},
        )
    api_env.executor = _StreamsHost()  # 过期回收不触 executor 方法；注册表在位
    resp = await _open_stream(api_env, uid, tid, after=1)
    try:
        meta = await _next_frame(resp)
        assert meta["data"]["status"] == "running"

        assert await reclaim_once(api_env) == 1
        flipped, queued = await _frames(resp, 2)
        # 帧序=事件序：status_changed(queued) 带水位键，round_queued 紧随其后
        assert (flipped["id"], flipped["event"]) == (2, "status_changed")
        assert flipped["data"] == {
            "status": "queued",
            "abort_reason": None,
            "event_sequence": 2,
        }
        assert (queued["id"], queued["event"]) == (3, "queued")
        assert set(queued["data"]) == {"round_id", "event_sequence"}
        assert queued["data"]["event_sequence"] == 3
    finally:
        await resp.body_iterator.aclose()

    # 落库事实面：轮回 pending（可被再次领取）、任务 queued、事件对同序
    round_state = await _rows(
        pg,
        "SELECT state, attempt, lease_owner FROM task_rounds WHERE task_id = :t",
        {"t": tid},
    )
    assert [(r.state, r.attempt, r.lease_owner) for r in round_state] == [("pending", 1, None)]
    task_state = await _rows(pg, "SELECT status FROM tasks WHERE id = :t", {"t": tid})
    assert [t.status for t in task_state] == ["queued"]
    events = await _rows(
        pg,
        "SELECT sequence, type FROM task_events WHERE task_id = :t ORDER BY sequence",
        {"t": tid},
    )
    assert [(e.sequence, e.type) for e in events] == [
        (2, "status_changed"),
        (3, "round_queued"),
    ]


async def test_round_deadline_publishes_failed_frames_live(pg, api_env, monkeypatch):
    """④轮级 hard deadline 强制终局：bounded-stop 后写位事务提交 →
    status_changed(failed) + done(error) 帧实时到达。"""
    monkeypatch.setattr(te, "_ROUND_DEADLINE_STOP_TIMEOUT", 0.2)
    uid = await seed_task_user(pg, "t8b-pub-deadline@example.com")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:  # 水位对齐 + 租约过户到本实例（复核围栏）
        await conn.execute(text("UPDATE tasks SET event_sequence = 1 WHERE id = :t"), {"t": tid})
        await conn.execute(
            text("UPDATE task_rounds SET lease_owner = :iid WHERE task_id = :t"),
            {"iid": _instance_id(), "t": tid},
        )
    executor, transport = _wire_executor(api_env, renew_seconds=0.05, deadline_seconds=0.4)
    transport.ignore_abort = True  # abort 被忽略 → bounded-stop forced → failed 写位
    transport.release = threading.Event()  # 轮挂起，等待 deadline 收口

    resp = await _open_stream(api_env, uid, tid, after=1)
    try:
        meta = await _next_frame(resp)
        assert meta["data"]["status"] == "running"

        await executor.notify(tid)
        runner = asyncio.create_task(executor.run_pending())
        deadline_wait = asyncio.get_running_loop().time() + 10
        while not transport.written and asyncio.get_running_loop().time() < deadline_wait:
            await asyncio.sleep(0.02)
        assert transport.written, "prompt 未在时限内送达"

        failed, done = await _frames(resp, 2)
        assert (failed["id"], failed["event"]) == (2, "status_changed")
        assert failed["data"] == {
            "status": "failed",
            "abort_reason": "round_failed",
            "event_sequence": 2,
        }
        assert done["id"] is None
        assert done["data"] == {"finish_reason": "error", "usage": {}}

        await asyncio.wait_for(runner, 20)
    finally:
        await resp.body_iterator.aclose()
        transport.release.set()
        await asyncio.sleep(0.05)  # 分离任务（deadline 收口）清场


async def test_upload_sweep_publishes_failed_frame(pg, api_env):
    """⑤sweep upload_expired：status_changed(failed, upload_expired) 帧实时到达。"""
    uid = await seed_task_user(pg, "t8b-pub-sweep@example.com")
    pid = await seed_provider(pg, uid)
    tid = str(await seed_task_for_provider(pg, uid, pid, status="uploading"))
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tasks SET created_at = now() - interval '25 hours' WHERE id = :t"),
            {"t": tid},
        )
    api_env.executor = _StreamsHost()
    resp = await _open_stream(api_env, uid, tid, after=0)
    try:
        meta = await _next_frame(resp)
        assert meta["data"] == {"task_id": tid, "status": "uploading", "event_sequence": 0}

        assert await sweep_upload_ttl(api_env) == 1
        failed = await _next_frame(resp)
        assert (failed["id"], failed["event"]) == (1, "status_changed")
        assert failed["data"] == {
            "status": "failed",
            "abort_reason": "upload_expired",
            "event_sequence": 1,
        }
    finally:
        await resp.body_iterator.aclose()


async def test_terminator_publishes_aborted_frames(pg, api_env):
    """⑥terminator（kill switch 联动写点）：queued 任务 aborted(tool_revoked) →
    status_changed + round_cancelled(done aborted) 帧提交后到达。"""
    uid = await seed_task_user(pg, "t8b-pub-term@example.com")
    pid = await seed_provider(pg, uid)
    tid = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    async with pg.engine.begin() as conn:
        await conn.execute(text("UPDATE tasks SET event_sequence = 1 WHERE id = :t"), {"t": tid})
        await conn.execute(  # revision_tools 反查圈定面（0007 触发器对 superuser 放行）
            text(
                "INSERT INTO revision_tools (id, expert_revision_id, tool_id, version) "
                "SELECT gen_random_uuid(), expert_revision_id, 'check_code_style', '1' "
                "FROM tasks WHERE id = :t"
            ),
            {"t": tid},
        )
    api_env.executor = _StreamsHost()  # queued 分支不触 stop_round；注册表在位
    terminator = build_terminator(api_env, api_env.executor)
    resp = await _open_stream(api_env, uid, tid, after=1)
    try:
        meta = await _next_frame(resp)
        assert meta["data"]["status"] == "queued"

        receipt = await terminator("check_code_style", "1")
        assert receipt["aborted_task_ids"] == [tid]
        flipped, done = await _frames(resp, 2)
        assert (flipped["id"], flipped["event"]) == (2, "status_changed")
        assert flipped["data"] == {
            "status": "aborted",
            "abort_reason": "tool_revoked",
            "event_sequence": 2,
        }
        assert done["id"] is None
        assert done["data"] == {"finish_reason": "aborted", "usage": {}}
    finally:
        await resp.body_iterator.aclose()


async def test_reconcile_publishes_status_frame(pg, api_env):
    """⑦reconcile（僵尸收口写点）：running 挂 pending_terminal 且无活跃轮 →
    终态化 status_changed(aborted) 帧实时到达。"""
    uid = await seed_task_user(pg, "t8b-pub-rec@example.com")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tasks SET event_sequence = 1, pending_terminal = 'aborted' WHERE id = :t"),
            {"t": tid},
        )
        await conn.execute(
            text("UPDATE task_rounds SET state = 'cancelled' WHERE task_id = :t"), {"t": tid}
        )
    api_env.executor = _StreamsHost()
    resp = await _open_stream(api_env, uid, tid, after=1)
    try:
        meta = await _next_frame(resp)
        assert meta["data"]["status"] == "running"

        assert await reconcile_pending_terminal(api_env) == 1
        flipped = await _next_frame(resp)
        assert (flipped["id"], flipped["event"]) == (2, "status_changed")
        assert flipped["data"] == {
            "status": "aborted",
            "abort_reason": "user_cancel",
            "event_sequence": 2,
        }
    finally:
        await resp.body_iterator.aclose()
