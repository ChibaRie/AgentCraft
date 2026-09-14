"""T8b messages/events/SSE 补拉重连测试（Phase 6）。

纪律（对齐 test_v2_tasks_api / test_v2_task_executor）：API 面经 make_v2_runtime
+ 依赖 override（provider_env/api_env）+ 真实登录流；服务面经 role_engine 双会话
owner_tx（并发用例）；owner-RLS 种子一律 superuser；执行链 FakePiTransport 实例级
注入（不触 Docker）。SSE 消费形态：**直接调端点函数**（httpx ASGITransport 整包
缓冲响应体，无限 SSE 流会挂死——逐帧迭代 StreamingResponse.body_iterator 并以
aclose() 模拟客户端断连，触发 finally unsubscribe；JSON 端点仍走真实 httpx 客户
端）。帧断言解析 id/event/data/comment 四行。限流窗口经 monkeypatch LIMITS 钉
小值（每测试独立克隆库窗口本互不影响）。
"""

import asyncio
import json
import threading
import uuid as _uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.api.v2 import task_sse
from backend.api.v2 import tasks as task_routes
from backend.errors import AgentCraftError, ErrorCode
from backend.v2 import task_service
from backend.v2.rate_limit import LIMITS
from backend.v2.session_service import V2AuthContext
from backend.v2.task_dispatcher import _instance_id, dispatch_once
from backend.v2.task_executor import RoundExecutor
from backend.v2.task_storage import TaskStorage
from backend.v2.task_streams import TaskStreamRegistry
from tests.conftest import APP_ROLE, FakePiTransport
from tests.test_v2_tasks_api import _create_task, _login, _seed_domain
from tests.v2_provider_helpers import seed_active_user, seed_provider, seed_task_for_provider
from tests.v2_task_helpers import owner_tx

# ---------------------------------------------------------------------------
# 种子 / 流消费助手
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_env(provider_env, tmp_path):
    """provider_env 基础上把任务物理存储根钉到 tmp（test_v2_tasks_api 同型；
    测试包助手函数可跨模块导入、fixture 不可复用——故本模块局部定义）。"""
    provider_env.storage = TaskStorage(tmp_path / "task-storage")
    return provider_env


class _StreamsHost:
    """最小 executor 替身：SSE 路由仅消费 ``runtime.executor.streams``（纯重放/
    实时注入用例无需执行链；重连对账用例另行接线真 RoundExecutor）。"""

    def __init__(self) -> None:
        self.streams = TaskStreamRegistry()


@pytest.fixture
async def stream_env(api_env):
    """SSE 流用例入口：executor 句柄位以注册表替身补位。"""
    api_env.executor = _StreamsHost()
    return api_env


async def _make_ready(pg, uid: str, pid: str) -> str:
    """queued 种子任务 → ready 形态（种子轮 settled + 水位对齐生产不变量——
    种子直插 message(event_sequence=1) 不推进 tasks.event_sequence）。"""
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE task_rounds SET state = 'settled' WHERE task_id = :t"), {"t": tid}
        )
        await conn.execute(
            text("UPDATE tasks SET status = 'ready', event_sequence = 1 WHERE id = :t"),
            {"t": tid},
        )
    return str(tid)


async def _reset_ready(pg, tid: str) -> None:
    """限流用例的轮间复位：pending 轮 settled + 任务回 ready（预算/幂等面不动）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET state = 'settled' WHERE task_id = :t AND state = 'pending'"
            ),
            {"t": tid},
        )
        await conn.execute(text("UPDATE tasks SET status = 'ready' WHERE id = :t"), {"t": tid})


async def _seed_all_event_types(pg, uid: str, pid: str) -> tuple[str, str]:
    """七种 task_events 全类型种子（D11 帧映射钉死载体）：@2..@8，水位对齐 8。
    返回 (task_id, round_id)。"""
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    async with pg.engine.begin() as conn:
        round_id = str(
            (
                await conn.execute(
                    text("SELECT id FROM task_rounds WHERE task_id = :t LIMIT 1"), {"t": tid}
                )
            ).scalar_one()
        )
        rows = [
            (
                2,
                "message_saved",
                {"message_id": str(_uuid.uuid4()), "event_sequence": 2, "author": "user"},
            ),
            (3, "status_changed", {"status": "running"}),
            (4, "round_running", {"round_id": round_id, "attempt": 1}),
            (5, "round_queued", {"round_id": round_id}),
            (
                6,
                "round_settled",
                {
                    "round_id": round_id,
                    "attempt": 1,
                    "finish_reason": "stop",
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            ),
            (7, "round_failed", {"round_id": round_id, "attempt": 2}),
            (8, "round_cancelled", {"round_id": round_id}),
        ]
        for seq, event_type, payload in rows:
            await conn.execute(
                text(
                    "INSERT INTO task_events (id, task_id, owner_id, sequence, type, "
                    "payload_json) VALUES (gen_random_uuid(), :t, :u, :s, :ty, CAST(:p AS jsonb))"
                ),
                {"t": tid, "u": uid, "s": seq, "ty": event_type, "p": json.dumps(payload)},
            )
        await conn.execute(text("UPDATE tasks SET event_sequence = 8 WHERE id = :t"), {"t": tid})
    return str(tid), round_id


def _auth_ctx(uid: str) -> V2AuthContext:
    """直接调端点函数的认证上下文替身（路由仅消费 user.id）。"""
    return V2AuthContext(user=SimpleNamespace(id=uid), session=SimpleNamespace())


async def _open_stream(rt, uid: str, tid: str, *, after: int = 0):
    """直接调用 SSE 端点（依赖显式传参，绕过 ASGI 整包缓冲）：返回
    StreamingResponse，body_iterator 逐帧产出。"""
    return await task_routes.stream_task_events(
        tid, after=after, user_ctx=_auth_ctx(uid), runtime=rt, _limit=None
    )


def _parse_frame(block: str) -> dict:
    """SSE 块解析：id/event/data 三行 + 注释行（心跳）。"""
    frame = {"id": None, "event": None, "data": None, "comment": None}
    for line in block.splitlines():
        if line.startswith(":"):
            frame["comment"] = line
        elif line.startswith("id:"):
            frame["id"] = int(line[3:].strip())
        elif line.startswith("event:"):
            frame["event"] = line[6:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            frame["data"] = json.loads(raw) if raw else None
    return frame


async def _next_frame(resp, timeout: float = 10.0) -> dict:
    """消费下一帧（超时护栏——流卡死即测试失败而非挂死）。"""

    async def read() -> dict:
        return _parse_frame(await resp.body_iterator.__anext__())

    return await asyncio.wait_for(read(), timeout)


async def _frames(resp, n: int, timeout: float = 10.0) -> list[dict]:
    return [await _next_frame(resp, timeout) for _ in range(n)]


async def _scalar(pg, sql: str, params: dict | None = None):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _rows(pg, sql: str, params: dict | None = None):
    async with pg.engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).all()


async def _wait_status(pg, tid: str, status: str, timeout: float = 10.0) -> None:
    """轮询任务状态到位（FakePi 轮 settle 为异步执行链）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    current = None
    while asyncio.get_running_loop().time() < deadline:
        current = await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", {"t": tid})
        if current == status:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务未在时限内到达 {status}（当前 {current}）")


# ---------------------------------------------------------------------------
# POST /messages：202 形态 / 忙闸 / 幂等重放（无论当前状态）
# ---------------------------------------------------------------------------


async def test_messages_send_202_shape_busy_gate_and_events(pg, api_env):
    uid, pid, _ = await _seed_domain(pg, "t8b-send@example.com")
    client = await _login(pg, "t8b-send@example.com")
    tid = await _make_ready(pg, uid, pid)

    resp = await client.post(
        f"/api/v2/tasks/{tid}/messages",
        json={"content": "下一句话"},
        headers={"Idempotency-Key": "t8b-send-1"},
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    assert set(data.keys()) == {"message", "event_sequence", "round_id"}
    assert set(data["message"].keys()) == {"id", "event_sequence"}
    assert data["message"]["event_sequence"] == 2  # 水位 1 → 消息 2
    assert data["event_sequence"] == 4  # round_queued 序
    assert data["round_id"]

    # DB 钉死：任务 ready→queued；新轮 pending 且 source_message_id=新消息；
    # 事件序 message_saved@2 → status_changed@3 → round_queued@4
    async with pg.engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM tasks WHERE id = :t"), {"t": tid})
        ).scalar_one()
        round_row = (
            await conn.execute(
                text(
                    "SELECT state, source_message_id FROM task_rounds "
                    "WHERE task_id = :t ORDER BY created_at DESC LIMIT 1"
                ),
                {"t": tid},
            )
        ).first()
        events = (
            await conn.execute(
                text("SELECT sequence, type FROM task_events WHERE task_id = :t ORDER BY sequence"),
                {"t": tid},
            )
        ).all()
    assert status == "queued"
    assert round_row.state == "pending"
    assert str(round_row.source_message_id) == data["message"]["id"]
    assert [(e.sequence, e.type) for e in events] == [
        (2, "message_saved"),
        (3, "status_changed"),
        (4, "round_queued"),
    ]

    # 活跃轮在握：新 key 再发 → 429 TASK_ROUND_BUSY + Retry-After
    busy = await client.post(
        f"/api/v2/tasks/{tid}/messages",
        json={"content": "再发一句"},
        headers={"Idempotency-Key": "t8b-send-2"},
    )
    assert busy.status_code == 429
    assert busy.json()["error"]["code"] == "TASK_ROUND_BUSY"
    assert int(busy.headers["Retry-After"]) >= 1


async def test_messages_idempotent_replay_regardless_of_state(pg, api_env):
    """Sup §1.2:25 钉死：幂等命中重放无论当前任务状态（首发送提交后任务已
    queued 且活跃轮在握，同 key 重放仍 202 原响应）。"""
    uid, pid, _ = await _seed_domain(pg, "t8b-idem@example.com")
    client = await _login(pg, "t8b-idem@example.com")
    tid = await _make_ready(pg, uid, pid)
    body = {"content": "幂等重放"}

    first = await client.post(
        f"/api/v2/tasks/{tid}/messages", json=body, headers={"Idempotency-Key": "t8b-idem-1"}
    )
    assert first.status_code == 202, first.text

    replay = await client.post(
        f"/api/v2/tasks/{tid}/messages", json=body, headers={"Idempotency-Key": "t8b-idem-1"}
    )
    assert replay.status_code == 202
    assert replay.json() == first.json()

    # 同 key 异载荷 → 409 IDEMPOTENCY_CONFLICT；缺 key → 400
    conflict = await client.post(
        f"/api/v2/tasks/{tid}/messages",
        json={"content": "换一句话"},
        headers={"Idempotency-Key": "t8b-idem-1"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    nokey = await client.post(f"/api/v2/tasks/{tid}/messages", json=body)
    assert nokey.status_code == 400


# ---------------------------------------------------------------------------
# 并发双发：恰一 202 一 429（预检 + 唯一索引双保险）
# ---------------------------------------------------------------------------


async def test_concurrent_double_send_exactly_one_202(pg, role_engine):
    """role_engine 双会话并发 send_message：任务行锁串行化下预检裁决——
    恰一 202 一 429 TASK_ROUND_BUSY；账面恰一条新消息 + 一条活跃轮。"""
    uid = await seed_active_user(pg, "t8b-concurrent@example.com")
    pid = await seed_provider(pg, uid, is_default=True)
    tid = await _make_ready(pg, uid, str(pid))
    eng1, eng2 = role_engine(APP_ROLE), role_engine(APP_ROLE)

    async def one(engine):
        async with owner_tx(engine, uid) as db:
            return await task_service.send_message(db, owner_id=uid, task_id=tid, content="并发")

    try:
        first, second = await asyncio.gather(one(eng1), one(eng2), return_exceptions=True)
    finally:
        await eng1.dispose()
        await eng2.dispose()

    ok = [r for r in (first, second) if not isinstance(r, BaseException)]
    busy = [r for r in (first, second) if isinstance(r, AgentCraftError)]
    assert len(ok) == 1 and len(busy) == 1
    assert busy[0].code is ErrorCode.TASK_ROUND_BUSY
    assert busy[0].http_status == 429
    assert busy[0].headers == {"Retry-After": "5"}

    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_messages WHERE task_id = :t AND content = :c",
            {"t": tid, "c": "并发"},
        )
        == 1
    )
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_rounds WHERE task_id = :t AND state = 'pending'",
            {"t": tid},
        )
        == 1
    )
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", {"t": tid}) == "queued"


async def test_active_round_unique_index_maps_429(pg, role_engine, monkeypatch):
    """唯一索引即并发闸门（预检漏网形态）：预检之后、落库之前竞争轮变活跃 →
    flush 处 IntegrityError 按约束名 one_active_round_per_task 映射 429 +
    Retry-After；事务回滚无半账、不落消息。

    注入形态注记：竞争行须**先于** send_message 插入（携带 tasks FK 的 INSERT
    会取 FOR KEY SHARE——与 send_message 事务在握的 tasks 行 FOR UPDATE 冲突，
    而后者正 await 本注入，构成 PG 无法侦测的自死锁）；钩子内只做不触 FK 的
    ``UPDATE task_rounds SET state='pending'``（预检读到 cancelled 形态而放行，
    等价复现「预检后竞争轮上线」窗口）。"""
    uid = await seed_active_user(pg, "t8b-unique@example.com")
    pid = await seed_provider(pg, uid, is_default=True)
    tid = await _make_ready(pg, uid, str(pid))
    original_allocate = task_service._allocate_event_sequence

    async with pg.engine.begin() as conn:
        mid = (
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), :t, "
                    "(SELECT owner_id FROM tasks WHERE id = :t), 99, 'user', 'sabotage') "
                    "RETURNING id"
                ),
                {"t": tid},
            )
        ).scalar_one()
        competing_round_id = (
            await conn.execute(
                text(
                    "INSERT INTO task_rounds (id, task_id, owner_id, source_message_id, "
                    "state, lease_epoch, attempt) VALUES (gen_random_uuid(), :t, "
                    "(SELECT owner_id FROM tasks WHERE id = :t), :m, 'cancelled', 0, 0) "
                    "RETURNING id"
                ),
                {"t": tid, "m": mid},
            )
        ).scalar_one()

    async def activate_competing_round(db, task_uuid, count=1):
        async with pg.engine.begin() as conn:
            await conn.execute(
                text("UPDATE task_rounds SET state = 'pending' WHERE id = :r"),
                {"r": str(competing_round_id)},
            )
        return await original_allocate(db, task_uuid, count)

    monkeypatch.setattr(task_service, "_allocate_event_sequence", activate_competing_round)

    engine = role_engine(APP_ROLE)
    try:
        with pytest.raises(AgentCraftError) as ei:
            async with owner_tx(engine, uid) as db:
                await task_service.send_message(db, owner_id=uid, task_id=tid, content="竞态注入")
    finally:
        await engine.dispose()
    assert ei.value.code is ErrorCode.TASK_ROUND_BUSY
    assert ei.value.http_status == 429
    assert ei.value.headers == {"Retry-After": "5"}

    # 回滚无半账：本 send 的消息未落库；任务仍在 ready；竞争轮（独立提交）在握
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_messages WHERE task_id = :t AND content = :c",
            {"t": tid, "c": "竞态注入"},
        )
        == 0
    )
    assert await _scalar(pg, "SELECT status FROM tasks WHERE id = :t", {"t": tid}) == "ready"
    assert (
        await _scalar(
            pg,
            "SELECT count(*) FROM task_rounds WHERE task_id = :t AND state = 'pending'",
            {"t": tid},
        )
        == 1
    )


# ---------------------------------------------------------------------------
# GET /events：D11 帧映射钉死 + 空集快照分支 + 统一 404
# ---------------------------------------------------------------------------


async def test_events_mapping_pins_d11_all_types(pg, api_env):
    uid, pid, _ = await _seed_domain(pg, "t8b-d11@example.com")
    client = await _login(pg, "t8b-d11@example.com")
    tid, round_id = await _seed_all_event_types(pg, uid, pid)

    resp = await client.get(f"/api/v2/tasks/{tid}/events", params={"after": 1})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["snapshot"] == {"status": "queued", "event_sequence": 8}
    # round_running 无独立帧（D11）——@4 不出现；queued 帧持久重放（D11 选边）
    assert [e["sequence"] for e in data["events"]] == [2, 3, 5, 6, 7, 8]
    by_seq = {e["sequence"]: e for e in data["events"]}
    assert by_seq[2] == {
        "sequence": 2,
        "type": "message_saved",
        "message_id": by_seq[2]["message_id"],
        "event_sequence": 2,
        "author": "user",
    }
    assert len(by_seq[2]["message_id"]) == 36  # message_id 为合法 UUID 形态（弱引用可解析）
    assert "content" not in by_seq[2]  # message_saved 帧不带正文（D11）
    assert by_seq[3] == {
        "sequence": 3,
        "type": "status_changed",
        "status": "running",
        "abort_reason": None,
    }
    assert by_seq[5] == {"sequence": 5, "type": "queued", "round_id": round_id}
    assert by_seq[6] == {
        "sequence": 6,
        "type": "done",
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }
    assert by_seq[7] == {"sequence": 7, "type": "done", "finish_reason": "error", "usage": {}}
    assert by_seq[8] == {"sequence": 8, "type": "done", "finish_reason": "aborted", "usage": {}}


async def test_events_after_watermark_empty_with_snapshot(pg, api_env):
    """after > watermark → 事件空集 + 当前快照仍随附（Sup §1.2:27）；他人统一 404。"""
    uid, pid, _ = await _seed_domain(pg, "t8b-empty@example.com")
    client = await _login(pg, "t8b-empty@example.com")
    tid, _round_id = await _seed_all_event_types(pg, uid, pid)

    resp = await client.get(f"/api/v2/tasks/{tid}/events", params={"after": 999})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["events"] == []
    assert data["snapshot"] == {"status": "queued", "event_sequence": 8}

    await seed_active_user(pg, "t8b-empty-intruder@example.com")
    intruder = await _login(pg, "t8b-empty-intruder@example.com")
    seen_events = await intruder.get(f"/api/v2/tasks/{tid}/events")
    seen_messages = await intruder.get(f"/api/v2/tasks/{tid}/messages")
    assert seen_events.status_code == 404
    assert seen_events.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert seen_messages.status_code == 404


# ---------------------------------------------------------------------------
# GET /messages：分页边界
# ---------------------------------------------------------------------------


async def test_messages_pagination_boundaries(pg, api_env):
    uid, pid, _ = await _seed_domain(pg, "t8b-page@example.com")
    client = await _login(pg, "t8b-page@example.com")
    tid = await seed_task_for_provider(pg, uid, pid, status="ready")
    async with pg.engine.begin() as conn:
        for i in range(2, 7):
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), :t, :u, :s, 'assistant', :c)"
                ),
                {"t": tid, "u": uid, "s": i, "c": f"m{i}"},
            )

    default_all = await client.get(f"/api/v2/tasks/{tid}/messages")
    assert default_all.status_code == 200
    items = default_all.json()["data"]
    assert [m["event_sequence"] for m in items] == [1, 2, 3, 4, 5, 6]
    assert set(items[0].keys()) == {"id", "event_sequence", "author", "content", "created_at"}
    assert items[0]["content"] == "seed"  # 全量正文（非摘要）

    paged = await client.get(f"/api/v2/tasks/{tid}/messages", params={"after": 4, "limit": 2})
    assert [m["event_sequence"] for m in paged.json()["data"]] == [5, 6]
    assert all(m["content"] == f"m{m['event_sequence']}" for m in paged.json()["data"])

    beyond = await client.get(f"/api/v2/tasks/{tid}/messages", params={"after": 100})
    assert beyond.json()["data"] == []

    for params in ({"limit": 201}, {"limit": 0}, {"after": -1}):
        bad = await client.get(f"/api/v2/tasks/{tid}/messages", params=params)
        assert bad.status_code == 400, params
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# SSE：meta/重放/id 行、断连重连对账（验收⑤）、合并去重、降级帧、心跳
# ---------------------------------------------------------------------------


async def test_sse_stream_meta_replay_and_id_lines(pg, stream_env):
    """流建立序（重放面）：meta（初始 watermark，无 id 行）→ 持久帧升序带
    ``id: <event_sequence>``；round_running 不发帧；帧形状与 /events 一致。"""
    uid, pid, _ = await _seed_domain(pg, "t8b-sse-replay@example.com")
    tid, round_id = await _seed_all_event_types(pg, uid, pid)

    resp = await _open_stream(stream_env, uid, tid, after=1)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["x-accel-buffering"] == "no"
    assert resp.headers["cache-control"] == "no-cache"
    try:
        meta, *rest = await _frames(resp, 7)
        assert meta["event"] == "meta" and meta["id"] is None
        assert meta["data"] == {
            "task_id": str(tid),
            "status": "queued",
            "event_sequence": 8,
        }
        got = [(f["id"], f["event"]) for f in rest]
        assert got == [
            (2, "message_saved"),
            (3, "status_changed"),
            (5, "queued"),
            (6, "done"),
            (7, "done"),
            (8, "done"),
        ]
        assert "content" not in rest[0]["data"]
        assert rest[0]["data"]["author"] == "user"
        assert rest[2]["data"] == {"round_id": round_id}
    finally:
        await resp.body_iterator.aclose()  # 断连 → finally unsubscribe

    # 404 统一信封（流建立前 JSON 短路；缺失/他人/已删除同形）
    with pytest.raises(AgentCraftError) as ei:
        await _open_stream(stream_env, uid, str(_uuid.uuid4()))
    assert ei.value.code is ErrorCode.TASK_NOT_FOUND


def _wire_executor(rt):
    """真 RoundExecutor + FakePiTransport 实例级注入（test_v2_task_executor 同型）；
    instance_id 必须与 dispatch_once 一致（复核围栏 lease_owner 谓词）。"""
    streams = TaskStreamRegistry()
    executor = RoundExecutor(rt, streams=streams, instance_id=_instance_id())
    transport = FakePiTransport()

    async def fake_make_runtime(spec, extension_path):
        async def noop() -> None:
            return None

        return transport, noop

    async def fake_ensure_proxy(provider: str) -> None:
        return None

    executor._make_runtime = fake_make_runtime  # type: ignore[method-assign]
    executor.ensure_proxy = fake_ensure_proxy  # type: ignore[method-assign]
    rt.executor = executor
    return executor, transport


async def test_sse_reconnect_replay_no_gap_no_dup(pg, api_env):
    """验收⑤：FakePiTransport 轮进行中订阅 → 收 N 帧 → 断开 → 轮 settle →
    重连 ?after=<last> → 重放序列无缺无重；水位与 meta 快照钉死。"""
    uid, pid, rid = await _seed_domain(pg, "t8b-reconn@example.com")
    client = await _login(pg, "t8b-reconn@example.com")
    executor, transport = _wire_executor(api_env)

    created = await _create_task(client, rid, pid, idem="t8b-rc-create")
    assert created.status_code == 201, created.text
    tid = created.json()["data"]["task"]["id"]
    committed = await client.post(
        f"/api/v2/tasks/{tid}/input/commit",
        json={"manifest": []},
        headers={"Idempotency-Key": "t8b-rc-commit"},
    )
    assert committed.status_code == 200, committed.text

    transport.release = threading.Event()  # 轮挂起在 agent_start 前（轮进行中）
    assert await dispatch_once(api_env) == 1
    runner = asyncio.create_task(executor.run_pending())
    deadline = asyncio.get_running_loop().time() + 10
    while not transport.written and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert transport.written, "prompt 未在时限内送达"

    # 事件水位账：create 消息@1 → commit status_changed@2 + round_queued@3 →
    # dispatch status_changed@4 + round_running@5 →（放行后）message_saved@6 +
    # round_settled@7 + status_changed@8
    #
    # 连接 1：轮进行中订阅（after=0）→ meta（running/水位 5）+ 重放 4 帧
    resp1 = await _open_stream(api_env, uid, tid, after=0)
    try:
        meta1, saved1, flipped_q, queued, running = await _frames(resp1, 5)
        assert meta1["data"] == {"task_id": tid, "status": "running", "event_sequence": 5}
        assert (saved1["id"], saved1["event"]) == (1, "message_saved")
        assert saved1["data"]["author"] == "user" and "content" not in saved1["data"]
        assert (flipped_q["id"], flipped_q["event"]) == (2, "status_changed")
        assert flipped_q["data"]["status"] == "queued"
        assert (queued["id"], queued["event"]) == (3, "queued")  # D11 持久帧
        assert (running["id"], running["event"]) == (4, "status_changed")
        assert running["data"]["status"] == "running"
    finally:
        await resp1.body_iterator.aclose()  # 断开（last=4）

    transport.release.set()  # 放行 → 轮执行 → settle（事实面落库）
    await _wait_status(pg, tid, "ready")
    assert await asyncio.wait_for(runner, 20) == 1

    # 重连 ?after=4：重放 seq>4 无缺无重（round_running@5 无帧；6/7/8 恰一次）
    resp2 = await _open_stream(api_env, uid, tid, after=4)
    try:
        meta2, saved, done, flipped = await _frames(resp2, 4)
        assert meta2["event"] == "meta"
        assert meta2["data"] == {"task_id": tid, "status": "ready", "event_sequence": 8}
        assert [
            (saved["id"], saved["event"]),
            (done["id"], done["event"]),
            (flipped["id"], flipped["event"]),
        ] == [
            (6, "message_saved"),
            (7, "done"),
            (8, "status_changed"),
        ]
        assert saved["data"]["author"] == "assistant"
        assert "content" not in saved["data"]
        assert done["data"]["finish_reason"] == "stop"
        assert flipped["data"]["status"] == "ready"
        assert flipped["data"]["abort_reason"] is None
    finally:
        await resp2.body_iterator.aclose()

    # 对账收口：事实面序列恰为 1..8（重放面无缺）；消息正全量在 /messages
    events = await _rows(
        pg,
        "SELECT sequence, type FROM task_events WHERE task_id = :t ORDER BY sequence",
        {"t": tid},
    )
    assert [(e.sequence, e.type) for e in events] == [
        (1, "message_saved"),
        (2, "status_changed"),
        (3, "round_queued"),
        (4, "status_changed"),
        (5, "round_running"),
        (6, "message_saved"),
        (7, "round_settled"),
        (8, "status_changed"),
    ]


def test_sse_merge_drain_dedup_and_transient():
    """合并排空单元钉死：重放先行；缓冲持久帧 seq<=last_sent 丢一（重放双见）、
    新鲜 seq 收编推进水位；瞬态/降级帧（无序号）原样保留在队尾。"""

    async def scenario():
        replay = [
            (1, {"type": "status_changed", "status": "running", "abort_reason": None}),
            (
                2,
                {
                    "type": "message_saved",
                    "message_id": "m2",
                    "event_sequence": 2,
                    "author": "user",
                },
            ),
            (3, {"type": "done", "finish_reason": "stop", "usage": {}}),
        ]
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(
            {"type": "message_saved", "message_id": "m2", "event_sequence": 2, "author": "user"}
        )  # 重放双见 → 丢
        queue.put_nowait(
            {"type": "message_saved", "message_id": "m4", "event_sequence": 4, "author": "tool"}
        )  # 新鲜 → 收
        queue.put_nowait({"type": "text_delta", "delta": "z"})  # 瞬态 → 收（无 id）
        queue.put_nowait(
            {
                "type": "message_saved",
                "message_id": "",
                "event_sequence": None,
                "author": "assistant",
            }
        )  # T6b M6 降级帧形态
        merged, last_sent = task_sse._merge_stream_frames(replay, task_sse._drain_queue(queue), 0)
        assert last_sent == 4
        assert [(seq, frame["type"]) for seq, frame in merged] == [
            (1, "status_changed"),
            (2, "message_saved"),
            (3, "done"),
            (4, "message_saved"),
            (None, "text_delta"),
            (None, "message_saved"),
        ]

    asyncio.run(scenario())


async def test_sse_tolerates_degradation_frame_live(pg, stream_env):
    """T6b M6 交接：持久化失败降级帧 {type, message_id:""}（无 event_sequence）
    不炸 SSE 消费——无 id 行、载荷原样透传、连接继续可用。"""
    uid, pid, _ = await _seed_domain(pg, "t8b-degrade@example.com")
    tid = await seed_task_for_provider(pg, uid, pid, status="ready")
    streams = stream_env.executor.streams

    resp = await _open_stream(stream_env, uid, tid, after=0)
    try:
        meta = await _next_frame(resp)
        assert meta["event"] == "meta"

        consumer = asyncio.create_task(_next_frame(resp))
        await asyncio.sleep(0.1)  # 让生成器推进到实时等待点
        streams.publish(
            tid,
            {
                "type": "message_saved",
                "message_id": "",
                "event_sequence": None,
                "author": "assistant",
            },
        )
        degraded = await asyncio.wait_for(consumer, 5)
        assert degraded["id"] is None  # 降级帧无序号 → 无 id 行
        assert degraded["event"] == "message_saved"
        assert degraded["data"] == {
            "message_id": "",
            "event_sequence": None,
            "author": "assistant",
        }

        # 连接仍可用：后续正常帧照常投递
        consumer = asyncio.create_task(_next_frame(resp))
        await asyncio.sleep(0.1)
        streams.publish(tid, {"type": "text_delta", "delta": "继"})
        delta = await asyncio.wait_for(consumer, 5)
        assert delta["event"] == "text_delta" and delta["id"] is None
        assert delta["data"] == {"delta": "继"}
    finally:
        await resp.body_iterator.aclose()


async def test_sse_heartbeat_comment_line(pg, stream_env, monkeypatch):
    """15s 心跳注释行（测试注入 0.2s）：无帧期间发 ``: ping``，不携 id/event。"""
    monkeypatch.setattr(task_sse, "_SSE_HEARTBEAT_SECONDS", 0.2)
    uid, pid, _ = await _seed_domain(pg, "t8b-ping@example.com")
    tid = await seed_task_for_provider(pg, uid, pid, status="ready")

    resp = await _open_stream(stream_env, uid, tid, after=0)
    try:
        meta = await _next_frame(resp)
        assert meta["event"] == "meta"  # 重放面为空 → meta 后直入实时等待
        ping = await _next_frame(resp, timeout=5)
        assert ping["comment"] == ": ping"
        assert ping["event"] is None and ping["id"] is None
    finally:
        await resp.body_iterator.aclose()


# ---------------------------------------------------------------------------
# 限流：send_message / sse_connect（D12 路由挂接）
# ---------------------------------------------------------------------------


async def test_rate_limit_send_message_429_and_replay_bypasses(pg, api_env, monkeypatch):
    """send_message 60/h/用户（T8b 裁决形态）：限流挂接在幂等 begin 之后——
    窗口耗尽后新 key 429 + Retry-After，而已获 key 的命中重放仍 202 原响应
    （不消耗新窗口，Sup §1.2:25 重放无条件）。"""
    monkeypatch.setitem(LIMITS, "send_message", (2, 3600))
    uid, pid, _ = await _seed_domain(pg, "t8b-rl-msg@example.com")
    client = await _login(pg, "t8b-rl-msg@example.com")
    tid = await _make_ready(pg, uid, pid)
    body = {"content": "限流窗口"}

    async def send(key: str):
        return await client.post(
            f"/api/v2/tasks/{tid}/messages", json=body, headers={"Idempotency-Key": key}
        )

    first = await send("t8b-rl-1")
    assert first.status_code == 202, first.text
    await _reset_ready(pg, tid)
    second = await send("t8b-rl-2")
    assert second.status_code == 202
    await _reset_ready(pg, tid)

    third = await send("t8b-rl-3")
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert int(third.headers["Retry-After"]) >= 1

    replay = await send("t8b-rl-1")  # 窗口已满：命中重放先于限流短路
    assert replay.status_code == 202
    assert replay.json() == first.json()


async def test_rate_limit_sse_connect_dependency_and_429(pg, stream_env, monkeypatch):
    """sse_connect 60/h/用户·任务：组合主体 [HMAC(user), HMAC(task)]——任一
    达限即整组 429（换任务不改 user 主体仍拒）；路由声明位钉死。"""
    monkeypatch.setitem(LIMITS, "sse_connect", (1, 3600))
    uid, pid, _ = await _seed_domain(pg, "t8b-rl-sse@example.com")
    client = await _login(pg, "t8b-rl-sse@example.com")
    tid = await seed_task_for_provider(pg, uid, pid, status="ready")
    ctx = _auth_ctx(uid)

    await task_routes._enforce_sse_connect_limit(
        tid, user_ctx=ctx, runtime=stream_env
    )  # 第 1 次：入窗（user+task 各一行）
    with pytest.raises(HTTPException) as ei:
        await task_routes._enforce_sse_connect_limit(tid, user_ctx=ctx, runtime=stream_env)
    assert ei.value.status_code == 429
    assert int(ei.value.headers["Retry-After"]) >= 1

    other = await seed_task_for_provider(pg, uid, pid, status="ready")
    with pytest.raises(HTTPException):
        await task_routes._enforce_sse_connect_limit(other, user_ctx=ctx, runtime=stream_env)

    # 路由级：限流依赖先于流建立——窗口满后 GET 直接 429 JSON（非 SSE）
    denied = await client.get(f"/api/v2/tasks/{tid}/events/stream")
    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert "retry-after" in denied.headers


def test_sse_route_declares_sse_connect_limiter():
    """路由声明位：/events/stream 必须挂接 sse_connect 限流依赖（D12）——
    签名级 Depends 落在 dependant.dependencies。"""
    route = next(r for r in task_routes.router.routes if r.path == "/tasks/{task_id}/events/stream")
    deps = {d.call for d in route.dependant.dependencies}
    assert task_routes._enforce_sse_connect_limit in deps
