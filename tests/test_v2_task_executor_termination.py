"""执行器 T6b 终止面（bounded-stop / terminator 真件 / 注销钩子 / 生产接线 / riders）。

Phase 9 T6 自 test_v2_task_executor.py 后半逐字搬移；共享助手见 v2_executor_helpers。
"""

import asyncio
import base64
import threading
import uuid as _uuid

from sqlalchemy import text

import backend.main as main_module
from backend.main import app, lifespan
from backend.v2 import deletion_service
from backend.v2 import task_executor as te
from backend.v2.runtime import owner_session
from backend.v2.security import hash_password
from backend.v2.task_dispatcher import _instance_id
from backend.v2.task_executor import (
    RoundExecutor,
    build_terminator,
)
from tests import v2_executor_helpers as _exec_helpers
from tests.v2_executor_helpers import (
    _drain_round_tasks,
    _make_executor,
    _poll_status,
    _rows,
    _run_round,
    _scalar,
    _seed_task_tool,
    _sync_event_watermark,
    _wait_prompt,
)
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import seed_running_task, seed_task_user

# rt 夹具发现形态：以赋值别名引入（admin_env 同款惯例）。
rt = _exec_helpers.rt

_DIRECT_INSTANCE = "exec-test"  # seed_running_task 的 lease_owner

# outbox/限流信封材料（仅测试；与 test_v2_password_flows._KEY_MATERIAL 同值同源）
_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()


# ---------------------------------------------------------------------------
# T6b 终止面（bounded-stop / terminator 真件 / 注销钩子 / 生产接线 / riders）
# ---------------------------------------------------------------------------


# ---------- RIDER B（T6a 审查 I-2）：实时 message_saved 帧剥正文（D11） ----------


async def test_message_saved_frame_strips_content(pg, rt):
    """实时 message_saved 帧键集 = {type, message_id, event_sequence, author}——
    content 剥离（正文走 GET /messages，T8a SSE 消费）；重放面（task_events
    落库载荷）与事实面（task_messages.content）形状不受影响。"""
    uid = await seed_task_user(pg, "rider-b@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    await _sync_event_watermark(pg, tid)
    executor, streams, _transports = _make_executor(rt)
    queue = streams.register(tid)

    assert await _run_round(rt, executor, tid) == 1
    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    saved = [f for f in frames if f["type"] == "message_saved"]
    assert len(saved) == 1
    assert set(saved[0].keys()) == {"type", "message_id", "event_sequence", "author"}
    assert "content" not in saved[0]
    # 重放面不受影响：task_events.message_saved 载荷仍为三键（无 content）
    ev = await _rows(
        pg,
        "SELECT payload_json FROM task_events WHERE task_id = :t AND type = 'message_saved'",
        t=tid,
    )
    assert set(ev[0].payload_json.keys()) == {"message_id", "event_sequence", "author"}
    # 事实面正文完整落库
    assert (
        await _scalar(
            pg,
            "SELECT content FROM task_messages WHERE task_id = :t AND author = 'assistant'",
            t=tid,
        )
        == "seed"
    )


# ---------- stop_round（D4/D7f bounded-stop）----------


async def test_stop_round_graceful_when_round_closes(pg, rt):
    """abort 帧 → agent aborted 帧序 → 既有 settle 路径收轮 → graceful/stopped=true；
    stop_round 本身分文不写（轮 settled/任务 ready 均由 settle 落位）。"""
    uid = await seed_task_user(pg, "stop-g@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    executor, _streams, transports = _make_executor(rt)
    transports[0].release = threading.Event()

    await executor.notify(tid)
    run_task = asyncio.create_task(executor.run_pending())
    await _wait_prompt(transports[0])
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)

    receipt = await executor.stop_round(rid, reason="test", timeout=10.0)
    assert receipt == {"round_id": rid, "mode": "graceful", "stopped": True}
    await asyncio.wait_for(run_task, timeout=20)
    assert await _scalar(pg, "SELECT state FROM task_rounds WHERE id = :r", r=rid) == "settled"
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "ready"
    assert rid not in executor._rounds  # 登记面随收口弹出
    transports[0].release.set()
    await _drain_round_tasks(transports[0])


async def test_stop_round_forced_on_timeout_calls_engine_stop(pg, rt):
    """abort 无响应（ignore_abort）→ graceful 超时 → forced：engine.stop 已调
    （transport closed）；轮保持 running（终态由调用方条件 UPDATE 写位）。"""
    uid = await seed_task_user(pg, "stop-f@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    executor, _streams, transports = _make_executor(rt)
    transports[0].ignore_abort = True
    transports[0].release = threading.Event()

    await executor.notify(tid)
    run_task = asyncio.create_task(executor.run_pending())
    await _wait_prompt(transports[0])
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)

    receipt = await executor.stop_round(rid, reason="test", timeout=0.3)
    assert receipt == {"round_id": rid, "mode": "forced", "stopped": True}
    assert transports[0].closed is True
    assert await _scalar(pg, "SELECT state FROM task_rounds WHERE id = :r", r=rid) == "running"
    await asyncio.wait_for(run_task, timeout=20)  # _EngineDied → finally 资源收口
    assert rid not in executor._rounds
    transports[0].release.set()
    await _drain_round_tasks(transports[0])


async def test_stop_round_abstains_when_round_not_executing(pg, rt):
    """与自然 settle 竞态 / 轮不在本实例：stopped=false 幂等静默，任务现态保持
    （不写 aborted——迁移表与 M5 联动，工具校验留给下轮）。"""
    uid = await seed_task_user(pg, "stop-r@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    await _sync_event_watermark(pg, tid)
    executor, _streams, _transports = _make_executor(rt)

    assert await _run_round(rt, executor, tid) == 1  # 自然 settle 先到
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)
    receipt = await executor.stop_round(rid, reason="test", timeout=0.2)
    assert receipt == {"round_id": rid, "mode": "graceful", "stopped": False}
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "ready"
    # 未知 round_id（未领取/他实例）同形态
    ghost = str(_uuid.uuid4())
    assert await executor.stop_round(ghost, reason="test", timeout=0.2) == {
        "round_id": ghost,
        "mode": "graceful",
        "stopped": False,
    }


# ---------- RIDER A（T6a 审查 I-1）：轮级 hard deadline 强制（D7f）----------


async def test_round_deadline_forces_failed_round_and_task(pg, rt, monkeypatch):
    """deadline 缩短注入 + release 挂轮：到期 → bounded-stop（abort 被忽略 →
    forced engine.stop）→ 轮 failed + 任务 failed(round_failed)（系统路径条件
    UPDATE）+ 终态档对称释放 + grant 失效 + 续约协程停止推进。"""
    monkeypatch.setattr(te, "_ROUND_DEADLINE_STOP_TIMEOUT", 0.3)
    uid = await seed_task_user(pg, "deadline@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    await _sync_event_watermark(pg, tid)
    # 预置意图位（abort API 的 running 形态）：deadline 翻转须连带清列（D19 终审对齐）
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tasks SET pending_terminal = 'aborted' WHERE id = :t"), {"t": tid}
        )
    executor, _streams, transports = _make_executor(rt, renew_seconds=0.05, deadline_seconds=0.4)
    transports[0].ignore_abort = True
    transports[0].release = threading.Event()

    await executor.notify(tid)
    run_task = asyncio.create_task(executor.run_pending())
    await _wait_prompt(transports[0])
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)
    renewal = executor._renewals[rid]

    assert await _poll_status(pg, tid, "failed") == "failed"
    rrow = await _rows(
        pg, "SELECT state, lease_owner, attempt FROM task_rounds WHERE id = :r", r=rid
    )
    assert (rrow[0].state, rrow[0].lease_owner, rrow[0].attempt) == ("failed", None, 1)
    # D19 清列：任务面翻转同置 pending_terminal=NULL（终审 T6b-M1）
    assert await _scalar(pg, "SELECT pending_terminal FROM tasks WHERE id = :t", t=tid) is None
    events = await _rows(
        pg,
        "SELECT sequence, type, payload_json FROM task_events WHERE task_id = :t ORDER BY sequence",
        t=tid,
    )
    assert [(e.type, e.payload_json) for e in events] == [
        ("status_changed", {"status": "failed", "reason": "round_failed"}),
        ("round_failed", {"round_id": rid, "attempt": 1}),
    ]
    # grant 失效（令牌弹出）+ 续约强制终局（协程退出；登记弹出由执行链 finally
    # 承载——run_task join 事件驱动等待，替代 _renewals 清空轮询，T7 §5.6）
    assert rid not in executor.tokens
    assert renewal.done() is True
    assert transports[0].closed is True  # forced engine.stop 已调用
    await asyncio.wait_for(run_task, timeout=20)
    assert rid not in executor._renewals
    transports[0].release.set()
    await _drain_round_tasks(transports[0])
    # 终态档对称释放：running + active 双释放、槽位 free、双账归零
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_reservations "
            "WHERE task_id = :t AND kind IN ('running','active') AND state = 'released'",
            t=tid,
        )
        == 2
    )
    usage = await _rows(
        pg, "SELECT active_tasks, running_tasks FROM user_quota_usage WHERE user_id = :u", u=uid
    )
    assert (usage[0].active_tasks, usage[0].running_tasks) == (0, 0)


# ---------- build_terminator（D4 kill switch 任务侧联动真件）----------


async def test_terminator_queued_task_aborted_tool_revoked(pg, rt):
    """queued：admin 圈定 → app 事务 pending round→cancelled + aborted(tool_revoked)
    + status_changed/round_cancelled 事件 + active 释放。"""
    uid = await seed_task_user(pg, "term-q@x.test")
    pid = await seed_provider(pg, uid)
    tid = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    await _seed_task_tool(pg, tid)
    executor, _streams, _transports = _make_executor(rt)
    terminator = build_terminator(rt, executor)

    receipt = await terminator("check_code_style", "1")
    assert receipt["stopped"] == 0
    assert receipt["aborted_task_ids"] == [tid]
    first = receipt["receipts"][0]
    assert first["task_id"] == tid and first["status_before"] == "queued"
    assert first["flipped"] is True
    trow = await _rows(pg, "SELECT status, abort_reason FROM tasks WHERE id = :t", t=tid)
    assert (trow[0].status, trow[0].abort_reason) == ("aborted", "tool_revoked")
    assert await _scalar(pg, "SELECT state FROM task_rounds WHERE task_id = :t", t=tid) == (
        "cancelled"
    )
    events = await _rows(
        pg,
        "SELECT type, payload_json FROM task_events WHERE task_id = :t ORDER BY sequence",
        t=tid,
    )
    assert [(e.type, e.payload_json) for e in events] == [
        ("status_changed", {"status": "aborted", "reason": "tool_revoked"}),
        (
            "round_cancelled",
            {
                "round_id": await _scalar(
                    pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid
                ),
                "reason": "tool_revoked",
            },
        ),
    ]
    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = :t AND kind = 'active'",
            t=tid,
        )
        == "released"
    )


async def test_terminator_running_task_stop_round_and_abort(pg, rt):
    """running：admin 圈定 → executor.stop_round（bounded；abort 被忽略 → forced）
    → app 事务 aborted(tool_revoked) + 活跃轮 cancelled + 对称释放。"""
    uid = await seed_task_user(pg, "term-r@x.test")
    pid = await seed_provider(pg, uid)
    tid = str(await seed_running_task(pg, uid, pid))
    await _seed_task_tool(pg, tid)
    executor, _streams, transports = _make_executor(rt)
    transports[0].ignore_abort = True
    transports[0].release = threading.Event()
    await executor.notify(tid)
    run_task = asyncio.create_task(executor.run_pending())
    await _wait_prompt(transports[0])

    terminator = build_terminator(rt, executor, stop_timeout=0.3)
    receipt = await terminator("check_code_style", "1")
    assert receipt["stopped"] == 1
    assert receipt["aborted_task_ids"] == [tid]
    first = receipt["receipts"][0]
    assert first["stop"]["mode"] == "forced" and first["stop"]["stopped"] is True
    assert first["flipped"] is True
    trow = await _rows(pg, "SELECT status, abort_reason FROM tasks WHERE id = :t", t=tid)
    assert (trow[0].status, trow[0].abort_reason) == ("aborted", "tool_revoked")
    assert await _scalar(pg, "SELECT state FROM task_rounds WHERE task_id = :t", t=tid) == (
        "cancelled"
    )
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_reservations "
            "WHERE task_id = :t AND kind IN ('running','active') AND state = 'released'",
            t=tid,
        )
        == 2
    )
    await asyncio.wait_for(run_task, timeout=20)  # 执行链 _EngineDied 收口
    transports[0].release.set()
    await _drain_round_tasks(transports[0])


async def test_terminator_reentry_already_in_state_idempotent(pg, rt):
    """already_in_state 重入幂等：已 aborted 任务不再进入圈定集（空回执不抛）；
    _terminate_one 对已终态任务直调同态成功（flipped=False）。"""
    uid = await seed_task_user(pg, "term-i@x.test")
    pid = await seed_provider(pg, uid)
    tid = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    await _seed_task_tool(pg, tid)
    executor, _streams, _transports = _make_executor(rt)
    terminator = build_terminator(rt, executor)

    first = await terminator("check_code_style", "1")
    assert first["aborted_task_ids"] == [tid]
    second = await terminator("check_code_style", "1")
    assert second == {"stopped": 0, "aborted_task_ids": [], "receipts": []}
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "aborted"
    async with owner_session(rt, uid) as db:
        outcome = await te._terminate_one(db, task_id=tid, owner_id=uid)
    assert outcome == {"flipped": False}


# ---------- D18 容错：任务行被删后执行器静默弃权 ----------


async def test_executor_abstains_after_task_row_deleted(pg, rt):
    """轮进行中任务行被物理删（D18 注销）：事实帧/续约/settle 全部围栏出局，
    执行链照常走完不抛，资源照收（令牌弹出/引擎停止/登记弹出）。"""
    uid = await seed_task_user(pg, "gone@x.test")
    pid = await seed_provider(pg, uid)
    tid = str(await seed_running_task(pg, uid, pid))
    await _sync_event_watermark(pg, tid)
    executor, _streams, transports = _make_executor(rt)
    transports[0].release = threading.Event()
    await executor.notify(tid)
    run_task = asyncio.create_task(executor.run_pending())
    await _wait_prompt(transports[0])
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)
    assert rid in executor.tokens

    async with pg.engine.begin() as conn:
        await conn.execute(text("DELETE FROM tasks WHERE id = :t"), {"t": tid})  # CASCADE 全连带
    transports[0].release.set()
    await asyncio.wait_for(run_task, timeout=20)  # 全链围栏弃权，不抛
    assert await _scalar(pg, "SELECT count(*) FROM tasks WHERE id = :t", t=tid) == 0
    assert await _scalar(pg, "SELECT count(*) FROM task_rounds WHERE id = :r", r=rid) == 0
    assert rid not in executor.tokens
    assert rid not in executor._rounds
    assert transports[0].closed is True


# ---------- TERMINATE_TASKS_HOOK（D18 注销物理删）----------


async def test_terminate_tasks_hook_physical_purge(pg, rt, monkeypatch):
    """服务级 request_deletion 直驱（:248 调用点真件）：① fire-and-forget 放弃
    通知（abort 帧已发）；② 同事务活跃轮 cancelled + 三本账释放 + 任务行物理删
    （CASCADE 连带）；③ post-commit 物理删 task-storage；provider DELETE 随后
    通过（RESTRICT 解除）+ sweep 继续。"""
    monkeypatch.setattr(te, "_hook_runtime", lambda: rt)
    # outbox 信封密钥（neutralize_v2_env 钉空 → 用例级注入；test_v2_deletion_flow 同材料）
    monkeypatch.setenv("EMAIL_OUTBOX_ENCRYPTION_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", _KEY_MATERIAL)
    uid = await seed_task_user(pg, "hook@example.com")
    pid = await seed_provider(pg, uid)
    tid_running = str(await seed_running_task(pg, uid, pid))
    tid_queued = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    await _sync_event_watermark(pg, tid_running)
    executor, _streams, transports = _make_executor(rt, renew_seconds=0.2)
    rt.executor = executor
    transports[0].release = threading.Event()
    await executor.notify(tid_running)
    run_task = asyncio.create_task(executor.run_pending())
    await _wait_prompt(transports[0])
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid_running)
    # 物理树预置（post-commit 清理断言物）
    tree_file = rt.storage.input_path(tid_queued, _uuid.uuid4().hex)
    tree_file.parent.mkdir(parents=True, exist_ok=True)
    tree_file.write_text("x", encoding="utf-8")
    assert tree_file.exists()

    pw_hash = hash_password("right-pw-1")
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET password_hash = :h WHERE id = :u"), {"h": pw_hash, "u": uid}
        )
    body = await deletion_service.request_deletion(
        rt,
        user_id=uid,
        status="active",
        password_hash=pw_hash,
        mfa_secret_enc=None,
        email="hook@example.com",
        password="right-pw-1",
        totp_code=None,
        idem_key="hook-1",
        idem_hash="0" * 64,
    )
    assert body["data"]["status"] == "deleting"

    # ① fire-and-forget 放弃通知（不等待 bounded-stop）
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline and not transports[0].aborted:
        await asyncio.sleep(0.05)
    assert transports[0].aborted is True
    # ② 任务行物理删（CASCADE 连带 files/messages/rounds/events/reservations）
    for sql in (
        "SELECT count(*) FROM tasks WHERE owner_id = :u",
        "SELECT count(*) FROM task_reservations WHERE user_id = :u",
        "SELECT count(*) FROM task_rounds WHERE owner_id = :u",
    ):
        assert await _scalar(pg, sql, u=uid) == 0
    usage = await _rows(
        pg,
        "SELECT active_tasks, running_tasks, retained_storage_bytes "
        "FROM user_quota_usage WHERE user_id = :u",
        u=uid,
    )
    assert (usage[0].active_tasks, usage[0].running_tasks, usage[0].retained_storage_bytes) == (
        0,
        0,
        0,
    )
    slot = await _rows(pg, "SELECT state, task_id FROM platform_slots WHERE slot_no = 1")
    assert slot[0].state == "free" and slot[0].task_id is None
    # ③ post-commit 物理删 task-storage
    assert not tree_file.exists()
    # provider DELETE 通过 + sweep 继续（deadline 回拨后 sweep_expired 处理本用户）
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deletion_deadline_at = now() - interval '1 day' WHERE id = :u"),
            {"u": uid},
        )
    assert await deletion_service.sweep_expired(rt) == 1
    assert await _scalar(pg, "SELECT count(*) FROM user_providers WHERE id = :p", p=pid) == 0
    # 在途轮静默弃权收口（行已删——D16 弃权语义）
    await asyncio.wait_for(run_task, timeout=20)
    assert rid not in executor.tokens
    transports[0].release.set()
    await _drain_round_tasks(transports[0])


async def test_terminate_hook_cleans_extension_script(pg, rt, monkeypatch):
    """终审 Important #1：注销钩子 ③ post-commit 物理删连带扩展脚本
    extensions/task-<id>.ts（delete_task_storage 范围不变，清理点负责）；
    他人扩展脚本不受影响。"""
    monkeypatch.setattr(te, "_hook_runtime", lambda: rt)
    uid = await seed_task_user(pg, "hook-ext@x.test")
    pid = await seed_provider(pg, uid)
    tid = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    gone = rt.storage.extension_path(tid)
    gone.write_text("ext", encoding="utf-8")
    keep = rt.storage.extension_path(str(_uuid.uuid4()))
    keep.write_text("keep", encoding="utf-8")
    assert gone.exists() and keep.exists()

    async with owner_session(rt, uid) as db:
        await te.terminate_tasks_hook(db, uid)

    assert not gone.exists()  # after_commit 物理删含扩展脚本
    assert keep.exists()
    assert await _scalar(pg, "SELECT count(*) FROM tasks WHERE owner_id = :u", u=uid) == 0


# ---------- main.py 生产接线（T6b）----------


async def test_main_lifespan_v2_wires_executor_and_drains(pg, rt, monkeypatch):
    """V2 模式：lifespan 构造 executor 挂 runtime + task-executor 常驻协程；
    关停 finally 取消排空（cancel 信号不吞——实测关停无挂起）。"""
    monkeypatch.setattr(main_module, "v2_runtime_from_settings", lambda: rt)
    handle: dict = {}
    async with lifespan(app):
        assert isinstance(rt.executor, RoundExecutor)
        assert rt.executor.instance_id == _instance_id()  # 与 dispatcher 同租约标识
        tasks = {t.get_name(): t for t in asyncio.all_tasks()}
        assert "task-executor" in tasks and "task-dispatcher" in tasks
        assert not tasks["task-executor"].done()
        await asyncio.sleep(0.1)  # 让 dispatcher 首个周期跑完——取消落在 sleep 而非 DB 中途
        handle["t"] = tasks["task-executor"]
        handle["rounds_empty"] = not rt.executor._rounds
    assert handle["rounds_empty"]
    assert handle["t"].done() and handle["t"].cancelled()  # 关停排空：无挂起


async def test_main_lifespan_v1_only_starts_no_v2_coroutines(monkeypatch):
    """V1-only（双 DSN 缺省）：零新 V2 协程（task-executor/task-dispatcher/
    outbox-dispatcher/deletion-sweeper 均不出现）——V1 块语句序零改动。"""
    monkeypatch.setattr(main_module, "v2_runtime_from_settings", lambda: None)
    async with lifespan(app):
        names = {t.get_name() for t in asyncio.all_tasks()}
        for v2_name in (
            "task-executor",
            "task-dispatcher",
            "outbox-dispatcher",
            "deletion-sweeper",
        ):
            assert v2_name not in names
