"""PiEngine 协议层测试（Engineering Spec §3.2/§7.2/§7.3 + rpc.md 0.84.3）。

帧语料来自 tests/fixtures/pi_frames/*.jsonl（步骤 1 手工实验录制）。
核心语义：
- prompt 的 response 只是受理 ACK；完成只认 agent_settled（§12 决策 #6）
- extension_ui_request 必须在 2s 内自动应答，否则扩展挂起（§3.2）
- response id 错配 = 容器状态污染 → needs_rebuild 标记（§7.2.1）
- 单行 JSON 坏了记日志跳过，不 crash（§7.3.5）
"""

import asyncio
import json
from pathlib import Path

import pytest

from backend.engine.pi_engine import PiCommandTimeoutError, PiEngine

FRAMES_DIR = Path(__file__).parent / "fixtures" / "pi_frames"


class FakeTransport:
    """内存传输：记录写入行，按脚本吐出 stdout 行。"""

    def __init__(self) -> None:
        self.written: list[dict] = []
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    def feed(self, frame: dict) -> None:
        self._lines.put_nowait(json.dumps(frame, ensure_ascii=False))

    def feed_raw(self, line: str) -> None:
        self._lines.put_nowait(line)

    def eof(self) -> None:
        self._lines.put_nowait(None)

    async def write_line(self, line: str) -> None:
        self.written.append(json.loads(line))

    async def readline(self) -> str | None:
        line = await self._lines.get()
        if line is None:
            self.closed = True
        return line

    async def close(self) -> None:
        self.closed = True


@pytest.fixture()
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture()
def engine(transport: FakeTransport) -> PiEngine:
    return PiEngine(task_id=1, transport=transport, command_timeout=2.0)


async def collect_events(engine: PiEngine) -> asyncio.Queue[dict]:
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def on_event(frame: dict) -> None:
        await queue.put(frame)

    engine.on_event(on_event)
    return queue


# ---------------------------------------------------------------------------
# 命令关联与 ACK 语义
# ---------------------------------------------------------------------------


async def test_prompt_ack_is_not_completion(engine: PiEngine, transport: FakeTransport):
    """ACK 帧只解决 pending Future；轮结束只认 agent_settled。"""
    events = await collect_events(engine)
    await engine.start()
    ack_task = asyncio.create_task(engine.send_prompt("你好，记住数字 42"))
    await asyncio.sleep(0)
    transport.feed({"id": "req_1", "type": "response", "command": "prompt", "success": True})
    ack = await asyncio.wait_for(ack_task, timeout=1)
    assert ack["success"] is True

    # ACK 之后、agent_settled 之前：轮尚未结束
    assert not engine.is_round_settled
    transport.feed({"type": "agent_settled"})
    settled = await asyncio.wait_for(events.get(), timeout=1)
    assert settled["type"] == "agent_settled"


async def test_command_ids_increment(engine: PiEngine, transport: FakeTransport):
    await engine.start()
    task1 = asyncio.create_task(engine.send_command({"type": "get_state"}))
    task2 = asyncio.create_task(engine.send_command({"type": "get_state"}))
    await asyncio.sleep(0)
    transport.feed(
        {"id": "req_1", "type": "response", "command": "get_state", "success": True, "data": {}}
    )
    transport.feed(
        {"id": "req_2", "type": "response", "command": "get_state", "success": True, "data": {}}
    )
    await asyncio.wait_for(asyncio.gather(task1, task2), timeout=1)
    assert [w["id"] for w in transport.written] == ["req_1", "req_2"]


async def test_command_timeout(engine: PiEngine, transport: FakeTransport):
    await engine.start()
    with pytest.raises(PiCommandTimeoutError):
        await engine.send_command({"type": "get_state"}, timeout=0.05)


async def test_abort_writes_frame_without_blocking(engine: PiEngine, transport: FakeTransport):
    await engine.start()
    await asyncio.wait_for(engine.abort(), timeout=0.5)
    assert transport.written[-1]["type"] == "abort"
    # 无 id 的 abort ACK 不应判为 id 错配
    transport.feed({"type": "response", "command": "abort", "success": True})
    await asyncio.sleep(0.05)
    assert not engine.needs_rebuild


# ---------------------------------------------------------------------------
# 行解析与分发
# ---------------------------------------------------------------------------


async def test_replays_recorded_frame_sequence(engine: PiEngine, transport: FakeTransport):
    """回放 faux_basic.jsonl 真实帧序，事件原样按序到达回调。"""
    events = await collect_events(engine)
    await engine.start()
    recorded = [
        json.loads(line)
        for line in (FRAMES_DIR / "faux_basic.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    # 跳过 response 帧（无对应 pending），其余应全部按序到达事件回调
    expected = [f for f in recorded if f.get("type") != "response"]
    for frame in recorded:
        transport.feed(frame)
    received = []
    for _ in range(len(expected)):
        received.append(await asyncio.wait_for(events.get(), timeout=1))
    assert [f.get("type") for f in received] == [f.get("type") for f in expected]


async def test_malformed_line_is_skipped(engine: PiEngine, transport: FakeTransport):
    events = await collect_events(engine)
    await engine.start()
    transport.feed_raw("这不是 JSON")
    transport.feed({"type": "agent_settled"})
    frame = await asyncio.wait_for(events.get(), timeout=1)
    assert frame["type"] == "agent_settled"
    assert not engine.needs_rebuild


async def test_id_mismatch_marks_rebuild(engine: PiEngine, transport: FakeTransport):
    await engine.start()
    transport.feed({"id": "req_unknown", "type": "response", "command": "prompt", "success": True})
    await asyncio.sleep(0.05)
    assert engine.needs_rebuild


# ---------------------------------------------------------------------------
# extension_ui_request 自动应答（§3.2：2s 内，否则扩展挂起）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("confirm", {"confirmed": False}),
        ("select", {"cancelled": True}),
        ("input", {"cancelled": True}),
        ("editor", {"cancelled": True}),
    ],
)
async def test_ui_dialog_auto_response(
    engine: PiEngine, transport: FakeTransport, method: str, expected: dict
):
    await engine.start()
    transport.feed({"type": "extension_ui_request", "id": "uuid-1", "method": method, "title": "t"})
    for _ in range(50):
        await asyncio.sleep(0.01)
        replies = [w for w in transport.written if w.get("type") == "extension_ui_response"]
        if replies:
            break
    replies = [w for w in transport.written if w.get("type") == "extension_ui_response"]
    assert replies, f"{method} 未在时限内应答"
    assert replies[0]["id"] == "uuid-1"
    for key, value in expected.items():
        assert replies[0][key] is value


async def test_ui_notify_is_ignored(engine: PiEngine, transport: FakeTransport):
    await engine.start()
    transport.feed(
        {"type": "extension_ui_request", "id": "uuid-2", "method": "notify", "message": "hi"}
    )
    await asyncio.sleep(0.1)
    assert not [w for w in transport.written if w.get("type") == "extension_ui_response"]
    assert not engine.needs_rebuild


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


async def test_stop_closes_transport_and_reader(engine: PiEngine, transport: FakeTransport):
    await engine.start()
    assert engine._reader_task is not None and not engine._reader_task.done()
    await engine.stop()
    assert transport.closed
    await asyncio.sleep(0.05)
    assert engine._reader_task is None or engine._reader_task.done()
