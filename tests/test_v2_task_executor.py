"""T6a/T6b RoundExecutor 测试（Phase 6，D16 会话编排 + D17 令牌 + D20 续约 +
T6b 终止面：bounded-stop/terminator 真件/注销钩子/生产接线）。

纪律（对齐 T5 测试与 Phase 6 计划）：owner-RLS 种子走 superuser
（tests/v2_task_helpers / v2_provider_helpers）；被测 executor 一律经
make_v2_runtime 真实 app/admin 双 role 执行——禁 superuser 直跑；断言
task_events 落库序列而非实时 SSE（实时面仅帧序断言）；FakePiTransport 全程
（实例级注入假 _make_runtime/ensure_proxy，不触 Docker）；时钟/租约用 DB 侧
回拨构造。executor 实例生命周期由各用例自管（conftest 清理夹具兜底残留）。
"""

import asyncio
import base64
import re
import threading
import uuid as _uuid

import pytest
from jose import jwt
from sqlalchemy import text

import backend.main as main_module
from backend.config import get_settings
from backend.engine.extension_generator import ExtensionGenerator
from backend.main import app, lifespan
from backend.services.task_token import (
    TaskTokenInvalid as V1TaskTokenInvalid,
)
from backend.services.task_token import create_task_token, decode_task_token
from backend.v2 import deletion_service
from backend.v2 import task_executor as te
from backend.v2.runtime import owner_session
from backend.v2.security import hash_password
from backend.v2.task_dispatcher import _instance_id, dispatch_once, reclaim_once
from backend.v2.task_executor import (
    RoundExecutor,
    build_terminator,
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

# outbox/限流信封材料（仅测试；与 test_v2_password_flows._KEY_MATERIAL 同值同源）
_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()


@pytest.fixture
async def rt(pg, tmp_path):
    """app/admin 双 role 运行时（RLS 真实生效）；storage 根隔离到 tmp（扩展产物）。"""
    runtime = make_v2_runtime(pg)
    runtime.storage = TaskStorage(tmp_path / "task-storage")
    yield runtime
    runtime.close()


def _make_executor(rt, *, instance_id=_DIRECT_INSTANCE, renew_seconds=None, deadline_seconds=None):
    """构造 RoundExecutor + 实例级假件注入（FakePiTransport/无 Docker）。

    FakePiTransport 预先构造（单轮用例）——release/on_round_start 等闸门可在
    run_pending 启动前安装，防帧在闸门就位前流出。"""
    streams = TaskStreamRegistry()
    executor = RoundExecutor(
        rt,
        streams=streams,
        instance_id=instance_id,
        renew_seconds=renew_seconds,
        deadline_seconds=deadline_seconds,
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
    """等待 prompt 已送达假引擎（recheck 已过、装配完成）——事件驱动（T7 §5.6
    sleep 清理：write_line 置位 prompt_written，零轮询；超时护栏保留）。"""
    try:
        await asyncio.wait_for(transport.prompt_written.wait(), timeout)
    except asyncio.TimeoutError as exc:
        raise AssertionError("prompt 未在时限内送达") from exc


async def _drain_round_tasks(transport: FakePiTransport, *, timeout: float = 5.0) -> None:
    """等待假引擎的 detached 轮任务真实退出（T7 §5.6 sleep 清理）：release 置位
    后 parked 在门上的轮任务下一节拍即收尾——await 真实完成替代固定尾部 sleep
    （遗漏 release 时经 wait_for 5s 超时显式失败，不静默吞过）。"""
    pending = [t for t in transport.round_tasks if not t.done()]
    if not pending:
        return
    await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout)


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
    trow = await _rows(
        pg, "SELECT status, abort_reason, pending_terminal FROM tasks WHERE id = :t", t=tid
    )
    assert (trow[0].status, trow[0].abort_reason) == ("aborted", "user_cancel")
    assert trow[0].pending_terminal is None  # RIDER C（T6a 审查 M-1）：决定终态并清列
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


# ---------------------------------------------------------------------------
# T6b 终止面（bounded-stop / terminator 真件 / 注销钩子 / 生产接线 / riders）
# ---------------------------------------------------------------------------


async def _seed_task_tool(pg, task_id: str, tool_id: str = "check_code_style", version: str = "1"):
    """为任务的 expert_revision 补 revision_tools 行（seed_task_for_provider 不造
    工具行；0007 冻结触发器对 GUC 未设的 superuser 上下文放行）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO revision_tools (id, expert_revision_id, tool_id, version) "
                "SELECT gen_random_uuid(), expert_revision_id, :tool, :ver "
                "FROM tasks WHERE id = :t"
            ),
            {"t": task_id, "tool": tool_id, "ver": version},
        )


async def _poll_status(pg, task_id: str, want: str, *, timeout: float = 10.0) -> str:
    """轮询任务状态直至 want（deadline/异步收口路径的确定化等待）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    status = None
    while loop.time() < deadline:
        status = await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", t=task_id)
        if status == want:
            return status
        await asyncio.sleep(0.05)
    return status


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
