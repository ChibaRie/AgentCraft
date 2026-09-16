"""任务域 SSE 测试共享助手（Phase 9 T6 拆分自 test_v2_task_sse.py）。

种子/帧解析/流消费助手 + _StreamsHost / stream_env 夹具 + _wire_executor
（真 RoundExecutor + FakePiTransport 实例级注入）——被 test_v2_task_sse
（协议面）与 test_v2_task_sse_runtime_frames（实时保真推帧面）共同消费。
api_env 夹具统一由 v2_task_helpers 提供（Phase 9 T6 前置上移）。
"""

import asyncio
import json
import uuid as _uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from backend.api.v2 import tasks as task_routes
from backend.v2.session_service import V2AuthContext
from backend.v2.task_dispatcher import _instance_id
from backend.v2.task_executor import (
    RoundExecutor,
)
from backend.v2.task_streams import TaskStreamRegistry
from tests.conftest import FakePiTransport
from tests.v2_provider_helpers import seed_task_for_provider

# ---------------------------------------------------------------------------
# 种子 / 流消费助手
# ---------------------------------------------------------------------------


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


def _wire_executor(
    rt, *, renew_seconds: float | None = None, deadline_seconds: float | None = None
):
    """真 RoundExecutor + FakePiTransport 实例级注入（test_v2_task_executor 同型）；
    instance_id 必须与 dispatch_once 一致（复核围栏 lease_owner 谓词）。
    renew_seconds/deadline_seconds 透传（deadline 收口推帧用例注入缩短值）。"""
    streams = TaskStreamRegistry()
    executor = RoundExecutor(
        rt,
        streams=streams,
        instance_id=_instance_id(),
        renew_seconds=renew_seconds,
        deadline_seconds=deadline_seconds,
    )
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
