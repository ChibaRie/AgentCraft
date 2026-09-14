"""T6a RoundExecutor 核心测试（Phase 6，D16 会话编排 + D17 令牌 + D20 续约）。

纪律（对齐 T5 测试与 Phase 6 计划）：owner-RLS 种子走 superuser
（tests/v2_task_helpers / v2_provider_helpers）；被测 executor 一律经
make_v2_runtime 真实 app/admin 双 role 执行——禁 superuser 直跑；断言
task_events 落库序列而非实时 SSE（实时面仅帧序断言）；FakePiTransport 全程
（实例级注入假 _make_runtime/ensure_proxy，不触 Docker）；时钟/租约用 DB 侧
回拨构造。executor 实例生命周期由各用例自管（无全局接线——T6b 职责）。
"""

import asyncio
import re
import threading
import uuid as _uuid

import pytest
from jose import jwt
from sqlalchemy import text

from backend.config import get_settings
from backend.engine.extension_generator import ExtensionGenerator
from backend.services.task_token import (
    TaskTokenInvalid as V1TaskTokenInvalid,
)
from backend.services.task_token import create_task_token, decode_task_token
from backend.v2.runtime import owner_session
from backend.v2.task_dispatcher import _instance_id, dispatch_once, reclaim_once
from backend.v2.task_executor import (
    RoundExecutor,
    executor_loop,
    reconcile_pending_terminal,
)
from backend.v2.task_service import abort_task
from backend.v2.task_storage import TaskStorage
from backend.v2.task_streams import TaskStreamRegistry
from backend.v2.task_token import (
    TaskTokenInvalid,
    create_v2_task_token,
    decode_v2_task_token,
)
from tests.conftest import FakePiTransport
from tests.test_v2_runtime import make_v2_runtime
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import seed_running_task, seed_task_user

_DIRECT_INSTANCE = "exec-test"  # seed_running_task 的 lease_owner


@pytest.fixture
async def rt(pg, tmp_path):
    """app/admin 双 role 运行时（RLS 真实生效）；storage 根隔离到 tmp（扩展产物）。"""
    runtime = make_v2_runtime(pg)
    runtime.storage = TaskStorage(tmp_path / "task-storage")
    yield runtime
    runtime.close()


def _make_executor(rt, *, instance_id=_DIRECT_INSTANCE, renew_seconds=None):
    """构造 RoundExecutor + 实例级假件注入（FakePiTransport/无 Docker）。

    FakePiTransport 预先构造（单轮用例）——release/on_round_start 等闸门可在
    run_pending 启动前安装，防帧在闸门就位前流出。"""
    streams = TaskStreamRegistry()
    executor = RoundExecutor(
        rt, streams=streams, instance_id=instance_id, renew_seconds=renew_seconds
    )
    transport = FakePiTransport()
    transports = [transport]

    async def fake_make_runtime(spec, extension_path):
        async def noop() -> None:
            return None

        return transport, noop

    async def fake_ensure_proxy(provider: str) -> None:
        return None

    executor._make_runtime = fake_make_runtime  # type: ignore[method-assign]
    executor.ensure_proxy = fake_ensure_proxy  # type: ignore[method-assign]
    return executor, streams, transports


async def _scalar(pg, sql: str, **params):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one_or_none()


async def _rows(pg, sql: str, **params):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params)).all()


async def _run_round(rt, executor, task_id: str, *, timeout: float = 20.0) -> int:
    """notify + run_pending（超时护栏）；返回执行轮数。"""
    await executor.notify(task_id)
    return await asyncio.wait_for(executor.run_pending(), timeout)


async def _wait_prompt(transport: FakePiTransport, *, timeout: float = 10.0) -> None:
    """等待 prompt 已送达假引擎（recheck 已过、装配完成）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if transport.written:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("prompt 未在时限内送达")


async def _sync_event_watermark(pg, task_id: str) -> None:
    """种子助手直插 message(event_sequence=1) 不推进 tasks.event_sequence 水位
    （生产写路径全部经 _allocate_event_sequence，水位恒同步）——执行链测试前
    手工对齐生产不变量，防首次分配撞 task_message_event_sequence 唯一约束。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE tasks SET event_sequence = 1 WHERE id = :t"), {"t": task_id}
        )


# ---------- V2 任务令牌（D17） ----------


def test_v2_task_token_roundtrip_claims():
    tid, oid, rid = str(_uuid.uuid4()), str(_uuid.uuid4()), str(_uuid.uuid4())
    token = create_v2_task_token(
        task_id=tid, owner_id=oid, round_id=rid, lease_epoch=3, instance="inst-1"
    )
    assert decode_v2_task_token(token) == {
        "task_id": tid,
        "owner_id": oid,
        "round_id": rid,
        "lease_epoch": 3,
        "instance": "inst-1",
    }


def test_v2_task_token_rejects_missing_claim_and_cross_aud():
    tid, oid, rid = str(_uuid.uuid4()), str(_uuid.uuid4()), str(_uuid.uuid4())
    # claims 缺一即拒：手工签发缺 owner_id 的载荷
    key = get_settings().TASK_TOKEN_SECRET or get_settings().SECRET_KEY
    broken = jwt.encode(
        {
            "task_id": tid,
            "round_id": rid,
            "lease_epoch": 1,
            "instance": "inst-1",
            "aud": "agentcraft:v2-task-token",
        },
        key,
        algorithm="HS256",
    )
    with pytest.raises(TaskTokenInvalid):
        decode_v2_task_token(broken)
    # aud 信任域隔离：V1 令牌不可解 V2 面，V2 令牌不可解 V1 面（同密钥不同 aud）
    v1_token = create_task_token(7, instance="x", model_id="m")
    with pytest.raises(TaskTokenInvalid):
        decode_v2_task_token(v1_token)
    v2_token = create_v2_task_token(
        task_id=tid, owner_id=oid, round_id=rid, lease_epoch=1, instance="x"
    )
    with pytest.raises(V1TaskTokenInvalid):
        decode_task_token(v2_token)


# ---------- 正常轮：全链（dispatch → notify → 执行链 → settle → ready） ----------


async def test_normal_round_full_chain(pg, rt):
    uid = await seed_task_user(pg, "e1@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    executor, _streams, transports = _make_executor(rt, instance_id=_instance_id())
    rt.executor = executor

    assert await dispatch_once(rt) == 1  # T5 领轮 + notify
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)
    assert await _run_round(rt, executor, tid) == 1

    # 轮 settled / 任务 ready（系统路径条件 UPDATE；running→ready 合法边）
    rrow = await _scalar(pg, "SELECT state FROM task_rounds WHERE id = :r", r=rid)
    assert rrow == "settled"
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "ready"

    # assistant 事实帧落库（FakePiTransport 回显 outgoing 消息 = 初始消息原文）
    msgs = await _rows(
        pg,
        "SELECT author, content FROM task_messages WHERE task_id = :t ORDER BY event_sequence",
        t=tid,
    )
    assert [(m.author, m.content) for m in msgs] == [("user", "seed"), ("assistant", "seed")]

    # task_events 落库序列：status_changed(running) → round_running → message_saved
    # → round_settled → status_changed(ready)
    events = await _rows(
        pg,
        "SELECT sequence, type, payload_json FROM task_events WHERE task_id = :t ORDER BY sequence",
        t=tid,
    )
    assert [(e.sequence, e.type) for e in events] == [
        (1, "status_changed"),
        (2, "round_running"),
        (3, "message_saved"),
        (4, "round_settled"),
        (5, "status_changed"),
    ]
    assert events[2].payload_json["author"] == "assistant"
    assert events[3].payload_json["finish_reason"] == "stop"
    assert events[4].payload_json == {"status": "ready"}

    # 三账对称：轮账释放（running reservation released + 槽位 free + running_tasks 0）；
    # ready 非终态档 → active 保留
    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = :t AND kind = 'running'",
            t=tid,
        )
        == "released"
    )
    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = :t AND kind = 'active'",
            t=tid,
        )
        == "held"
    )
    slot = await _rows(pg, "SELECT state, task_id FROM platform_slots WHERE slot_no = 1")
    assert slot[0].state == "free" and slot[0].task_id is None
    usage = await _rows(
        pg, "SELECT active_tasks, running_tasks FROM user_quota_usage WHERE user_id = :u", u=uid
    )
    assert (usage[0].active_tasks, usage[0].running_tasks) == (1, 0)

    # grant 失效形态：令牌弹出登记表；引擎/容器回收；扩展落 task-storage/extensions/
    assert rid not in executor.tokens
    assert transports[0].closed is True
    assert rt.storage.extension_path(tid).exists()


async def test_no_subscriber_round_completes(pg, rt):
    """断连不中断：空注册表（无订阅者）照跑——实时面缺席不影响事实面。"""
    uid = await seed_task_user(pg, "e2@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    await _sync_event_watermark(pg, tid)
    executor, streams, _transports = _make_executor(rt)

    assert await _run_round(rt, executor, tid) == 1
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "ready"
    # 无订阅者：publish 零开销 no-op 后事件面完整（message_saved + round_settled + ready）
    assert await _scalar(pg, "SELECT count(*) FROM task_events WHERE task_id = :t", t=tid) == 3
    assert not streams._subs


async def test_stream_frame_order_and_facts(pg, rt):
    """实时订阅帧序：瞬态（text_delta）→ 事实（message_saved 带序号/作者）→ done。"""
    uid = await seed_task_user(pg, "e3@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    await _sync_event_watermark(pg, tid)
    executor, streams, _transports = _make_executor(rt)
    queue = streams.register(tid)

    assert await _run_round(rt, executor, tid) == 1
    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    types = [f["type"] for f in frames]
    # "seed" 按 3 字符分帧 → 2 个 text_delta；done 恒收尾
    assert types == ["text_delta", "text_delta", "message_saved", "done"]
    assert [f["delta"] for f in frames[:2]] == ["see", "d"]
    assert frames[2]["event_sequence"] == 2  # 种子水位 1 → 首个分配序号 2
    assert frames[2]["author"] == "assistant" and frames[2]["message_id"]
    assert frames[3]["finish_reason"] == "stop"

    # unsubscribe 后不再投递（幂等注销 + 空注册表 publish no-op）
    streams.unsubscribe(tid, queue)
    streams.publish(tid, {"type": "x"})
    assert queue.empty()


# ---------- settle 比对：KEY_VERSION_REVOKED ----------


async def test_settle_key_version_revoked_aborts_task(pg, rt):
    """provider_key_version 失配 → 任务 aborted(provider_key_revoked)，轮照常
    settled（Sup §9.7.3）；终态档释放 active + 轮账。"""
    uid = await seed_task_user(pg, "e4@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE user_providers SET key_version = 2 WHERE id = :p"), {"p": pid}
        )
    await _sync_event_watermark(pg, tid)
    executor, _streams, _transports = _make_executor(rt)

    assert await _run_round(rt, executor, tid) == 1
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)
    assert await _scalar(pg, "SELECT state FROM task_rounds WHERE id = :r", r=rid) == "settled"
    trow = await _rows(pg, "SELECT status, abort_reason FROM tasks WHERE id = :t", t=tid)
    assert (trow[0].status, trow[0].abort_reason) == ("aborted", "provider_key_revoked")
    events = await _rows(
        pg,
        "SELECT sequence, type, payload_json FROM task_events WHERE task_id = :t ORDER BY sequence",
        t=tid,
    )
    assert [(e.sequence, e.type) for e in events] == [
        (2, "message_saved"),
        (3, "round_settled"),
        (4, "status_changed"),
    ]
    assert events[2].payload_json == {"status": "aborted", "reason": "provider_key_revoked"}
    # 终态档：running + active 双释放；active_tasks 归零
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


# ---------- lease 围栏（D16 写侧围栏） ----------


async def test_lease_fence_abstains_all_writes(pg, rt):
    """人为推进 lease_epoch 后：事实帧与 settle 全部围栏出局——分文不写。"""
    uid = await seed_task_user(pg, "e5@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    await _sync_event_watermark(pg, tid)
    executor, _streams, transports = _make_executor(rt)
    transports[0].release = threading.Event()

    await executor.notify(tid)
    run_task = asyncio.create_task(executor.run_pending())
    await _wait_prompt(transports[0])
    # 并发 fence：epoch 1 → 6（superuser 直改，模拟 reclaim/并发收口后的围栏失配）
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE task_rounds SET lease_epoch = lease_epoch + 5 WHERE task_id = :t"),
            {"t": tid},
        )
    transports[0].release.set()
    await asyncio.wait_for(run_task, timeout=20)

    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)
    rrow = await _rows(pg, "SELECT state, lease_epoch FROM task_rounds WHERE id = :r", r=rid)
    assert (rrow[0].state, rrow[0].lease_epoch) == ("running", 6)
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "running"
    assert await _scalar(pg, "SELECT count(*) FROM task_events WHERE task_id = :t", t=tid) == 0
    assert await _scalar(pg, "SELECT count(*) FROM task_messages WHERE task_id = :t", t=tid) == 1
    assert (
        await _scalar(
            pg,
            "SELECT state FROM task_reservations WHERE task_id = :t AND kind = 'running'",
            t=tid,
        )
        == "held"
    )
    # 分文不写但资源照收：令牌弹出（settle 弃权路径的清理仍执行）
    assert rid not in executor.tokens


# ---------- D20 续约 ----------


async def test_renewal_extends_lease_while_round_running(pg, rt):
    """release 挂轮 + 短续约间隔：回拨 lease_expires_at 后被续约协程推进（round
    权威计时器 + slot 观测面同步推进）。"""
    uid = await seed_task_user(pg, "e6@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    await _sync_event_watermark(pg, tid)
    executor, _streams, transports = _make_executor(rt, renew_seconds=0.15)
    transports[0].release = threading.Event()

    await executor.notify(tid)
    run_task = asyncio.create_task(executor.run_pending())
    await _wait_prompt(transports[0])
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET lease_expires_at = now() - interval '10 seconds' "
                "WHERE id = :r"
            ),
            {"r": rid},
        )
    # 续约协程（0.15s 节拍）必须把回拨的 lease 推回未来，否则轮必被 reclaim 回收
    deadline = asyncio.get_running_loop().time() + 5.0
    renewed = False
    while asyncio.get_running_loop().time() < deadline:
        ttl = await _scalar(
            pg,
            "SELECT extract(epoch FROM (lease_expires_at - now())) FROM task_rounds WHERE id = :r",
            r=rid,
        )
        slot_ttl = await _scalar(
            pg,
            "SELECT extract(epoch FROM (leased_until - now())) FROM platform_slots "
            "WHERE task_id = :t AND state = 'leased'",
            t=tid,
        )
        if ttl is not None and ttl > 0 and slot_ttl is not None and slot_ttl > 0:
            renewed = True
            break
        await asyncio.sleep(0.05)
    assert renewed, "续约协程未推进 round.lease_expires_at / platform_slots.leased_until"
    transports[0].release.set()
    await asyncio.wait_for(run_task, timeout=20)
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "ready"


async def test_renewal_stops_after_settle(pg, rt):
    """续约协程随 settle 停止：登记表弹出 + settle 后 lease 不再被推进。"""
    uid = await seed_task_user(pg, "e7@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    await _sync_event_watermark(pg, tid)
    executor, _streams, _transports = _make_executor(rt, renew_seconds=0.15)
    rid = await _scalar(pg, "SELECT id::text FROM task_rounds WHERE task_id = :t", t=tid)

    assert await _run_round(rt, executor, tid) == 1
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid) == "ready"
    assert rid not in executor._renewals  # 续约协程已从登记表移除
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET lease_expires_at = now() - interval '10 seconds' "
                "WHERE id = :r"
            ),
            {"r": rid},
        )
    await asyncio.sleep(0.6)  # > 3 个续约节拍
    ttl = await _scalar(
        pg,
        "SELECT extract(epoch FROM (lease_expires_at - now())) FROM task_rounds WHERE id = :r",
        r=rid,
    )
    assert ttl is not None and ttl < 0  # 未被推进（保持回拨的过去值）


# ---------- 扩展生成器 int|str 签名（D17 申报例外） ----------


def test_extension_int_path_byte_identical(tmp_path):
    """V1 int 路径回归钉：数字字面量（无引号）+ task-<int>.ts 文件名。"""
    src = (
        ExtensionGenerator(tmp_path)
        .generate(7, [("check_code_style", "1")], "openai")
        .read_text(encoding="utf-8")
    )
    assert "const TASK_ID = 7;" in src
    assert 'const TASK_ID = "7"' not in src
    assert (tmp_path / "task-7.ts").exists()


def test_extension_uuid_task_id_generation(tmp_path):
    """V2 UUID 路径：带引号 TS 字符串字面量 + task-<uuid>.ts；模板区零残留。"""
    uid = str(_uuid.uuid4())
    path = ExtensionGenerator(tmp_path).generate(uid, [], "openai")
    src = path.read_text(encoding="utf-8")
    assert f'const TASK_ID = "{uid}";' in src
    assert path.name == f"task-{uid}.ts"
    assert re.search(r"__[A-Z_]+__", src) is None


# ---------- 僵尸对账（T5 审查交接义务）与 reclaim 链 ----------


async def test_pending_terminal_zombie_reconciled(pg, rt):
    """running + pending_terminal='aborted' + 无活跃轮 → 对账终态化 aborted +
    active/running 双释放。"""
    uid = await seed_task_user(pg, "e8@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET state = 'cancelled', lease_owner = NULL, "
                "lease_expires_at = NULL WHERE task_id = :t"
            ),
            {"t": tid},
        )
        await conn.execute(
            text("UPDATE tasks SET pending_terminal = 'aborted' WHERE id = :t"), {"t": tid}
        )

    assert await reconcile_pending_terminal(rt) == 1
    trow = await _rows(pg, "SELECT status, abort_reason FROM tasks WHERE id = :t", t=tid)
    assert (trow[0].status, trow[0].abort_reason) == ("aborted", "user_cancel")
    events = await _rows(
        pg, "SELECT type, payload_json FROM task_events WHERE task_id = :t ORDER BY sequence", t=tid
    )
    assert [(e.type, e.payload_json) for e in events] == [
        ("status_changed", {"status": "aborted", "reason": "user_cancel"})
    ]
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
    assert await reconcile_pending_terminal(rt) == 0  # 幂等：无第二候选


async def test_abort_round_close_chain_via_reclaim_and_reconcile(pg, rt):
    """abort(202) → reclaim 取消 cancelling 轮（container_alive=False）→ 对账按
    意图位终态化——T5 交接停留形态的全链收口。"""
    uid = await seed_task_user(pg, "e9@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    executor, _streams, _transports = _make_executor(rt)
    rt.executor = executor

    async with owner_session(rt, uid) as db:
        await abort_task(db, owner_id=uid, task_id=tid)
    assert await _scalar(pg, "SELECT pending_terminal FROM tasks WHERE id = :t", t=tid) == "aborted"

    assert await reclaim_once(rt) == 1  # 容器不在场 → cancelling 轮收口
    assert await reconcile_pending_terminal(rt) == 1
    trow = await _rows(pg, "SELECT status, abort_reason FROM tasks WHERE id = :t", t=tid)
    assert (trow[0].status, trow[0].abort_reason) == ("aborted", "user_cancel")
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_reservations "
            "WHERE task_id = :t AND kind IN ('running','active') AND state = 'released'",
            t=tid,
        )
        == 2
    )


async def test_executor_loop_dispatch_to_ready(pg, rt):
    """executor_loop 冒烟：notify 消费 + 对账兜底常态运转，dispatch 领轮 → ready。"""
    uid = await seed_task_user(pg, "e10@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    executor, _streams, _transports = _make_executor(rt, instance_id=_instance_id())
    rt.executor = executor
    loop_task = asyncio.create_task(executor_loop(rt, poll_seconds=0.05))
    try:
        assert await dispatch_once(rt) == 1
        deadline = asyncio.get_running_loop().time() + 15.0
        while asyncio.get_running_loop().time() < deadline:
            status = await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=tid)
            if status == "ready":
                break
            await asyncio.sleep(0.05)
        assert status == "ready"
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
