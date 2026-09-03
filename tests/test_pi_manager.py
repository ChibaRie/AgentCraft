"""PiEngineManager 测试：容器表、重播种、abort、重建（§7.2/§7.6）。

用脚本化假 Pi 传输（prompt → 回显整条 outgoing 消息）验证重播种语义，
不依赖真实容器；真实 faux 全链路在验收阶段覆盖。
"""

import asyncio
from pathlib import Path

from backend.config import Settings
from backend.engine.extension_generator import ExtensionGenerator
from backend.engine.pi_engine_manager import PiEngineManager
from tests.conftest import FakePiTransport


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        PI_RUNTIME="subprocess",
        HOST_DATA_ROOT=str(tmp_path / "data"),
        HOST_WORKSPACE_ROOT=str(tmp_path / "workspaces"),
        PI_ROUND_TIMEOUT_SECONDS=5,
    )


class SpyPersistence:
    def __init__(self) -> None:
        self.assistant: list[dict] = []
        self.tools: list[dict] = []

    async def persist_assistant(self, content: str, usage: dict) -> dict:
        self.assistant.append({"content": content, "usage": usage})
        return {"message_id": len(self.assistant), "content": content}

    async def persist_tool(self, tool_call_id, tool_name, content, is_error) -> dict:
        self.tools.append({"tool_call_id": tool_call_id})
        return {"message_id": 999}


SNAPSHOT = {
    "skills": [{"name": "s", "content": "角色：测试"}],
    "expert_persona": "严谨",
    "expert_methodology": "分步",
    "loaded_at": "2026-09-03T00:00:00Z",
}


def make_manager(tmp_path: Path, history: list[dict]):
    settings = make_settings(tmp_path)
    transports: list[FakePiTransport] = []
    manager = PiEngineManager(
        settings,
        history_fetcher=_fake_history_fetcher(history),
        extension_generator=ExtensionGenerator(tmp_path / "ext"),
    )
    manager._removal = []  # type: ignore[attr-defined]

    async def fake_make_runtime(spec, workdir_host, extension_path):
        transport = FakePiTransport()
        transports.append(transport)

        async def noop() -> None:
            return None

        manager._removal.append(spec)  # type: ignore[attr-defined]
        return transport, noop

    manager._make_runtime = fake_make_runtime  # type: ignore[method-assign]
    return manager, transports


async def _fake_history_fetcher(history: list[dict], task_id: int, limit: int) -> list[dict]:
    return history[-limit:]


def _fake_history_fetcher(history: list[dict]):
    async def fetch(task_id: int, limit: int) -> list[dict]:
        return history[-limit:]

    return fetch


async def drive_round(manager: PiEngineManager, transports, content: str):
    spy = SpyPersistence()
    events = []
    async for name, payload in manager.run_round(
        task_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
        content=content,
        persist_assistant=spy.persist_assistant,
        persist_tool=spy.persist_tool,
    ):
        events.append((name, payload))
    return events, spy


# ---------------------------------------------------------------------------
# 重播种（§7.6）
# ---------------------------------------------------------------------------


async def test_first_round_wraps_history(tmp_path):
    history = [
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "第二轮新消息"},
    ]
    manager, transports = make_manager(tmp_path, history)
    events, spy = await drive_round(manager, transports, "第二轮新消息")

    outgoing = transports[0].written[0]["message"]
    assert outgoing.startswith("[历史对话回顾]")
    assert "用户：第一轮问题" in outgoing
    assert "助手：第一轮回答" in outgoing
    assert "[当前消息]\n第二轮新消息" in outgoing
    assert not transports[0].written[0].get("streamingBehavior")

    done = events[-1]
    assert done[0] == "done" and done[1]["finish_reason"] == "stop"
    # 幻影轮防线：一轮恰好一条 assistant 落库
    assert len(spy.assistant) == 1
    assert spy.assistant[0]["content"] == outgoing


async def test_second_round_sends_only_current(tmp_path):
    history = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "回答一"},
    ]
    manager, transports = make_manager(tmp_path, history)
    await drive_round(manager, transports, "当前消息 A")
    # 重建前 needs_reseed 仍为 True（同容器第二轮不重播种——直接驱动第二次 run_round）
    await drive_round(manager, transports, "当前消息 B")
    second = transports[0].written[1]["message"]
    assert second == "当前消息 B", "正常轮只发当前消息，不重复嵌历史"


async def test_rebuild_after_engine_death_reseeds_again(tmp_path):
    history = [
        {"role": "user", "content": "旧消息"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "重建后新消息"},
    ]
    manager, transports = make_manager(tmp_path, history)
    await drive_round(manager, transports, "第一条")
    # 模拟容器死亡：EOF 使读取协程退出
    transports[0].emit_eof()
    await asyncio.sleep(0.05)
    await drive_round(manager, transports, "重建后第一条")
    assert len(transports) == 2, "引擎死亡后必须重建容器"
    outgoing = transports[1].written[0]["message"]
    assert outgoing.startswith("[历史对话回顾]"), "重建后的第一条消息必须重播种"
    assert "用户：旧消息" in outgoing


async def test_reseed_no_ghost_round(tmp_path):
    """重播种不单独发历史：一轮 prompt 只产生一条 assistant 落库。"""
    history = [
        {"role": "user", "content": "历史一"},
        {"role": "assistant", "content": "历史答一"},
        {"role": "user", "content": "新消息"},
    ]
    manager, transports = make_manager(tmp_path, history)
    _, spy = await drive_round(manager, transports, "新消息")
    assert len(transports[0].written) == 1, "重播种只发一条 prompt"
    assert len(spy.assistant) == 1


async def test_reseed_skipped_when_no_prior_history(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    await drive_round(manager, transports, "首条消息")
    outgoing = transports[0].written[0]["message"]
    assert outgoing == "首条消息", "无历史时不包回顾壳"


# ---------------------------------------------------------------------------
# abort / 重建标记 / 令牌
# ---------------------------------------------------------------------------


async def test_request_abort_bypasses_and_writes_abort(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    engine = await manager.ensure_container(
        task_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    engine.is_round_settled = False  # 模拟轮进行中
    await manager.request_abort(1)
    assert transports[0].aborted is True
    assert manager.has_active_round(1) is False or engine.is_round_settled is False


async def test_id_mismatch_rebuilds_on_next_ensure(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    engine = await manager.ensure_container(
        task_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    transports[0].emit({"id": "req_bogus", "type": "response", "command": "x", "success": True})
    await asyncio.sleep(0.05)
    assert engine.needs_rebuild is True
    rebuilt = await manager.ensure_container(
        task_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    assert rebuilt is not engine, "id 错配容器必须重建"


async def test_task_token_rotates_on_rebuild(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    await manager.ensure_container(
        task_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    token_a = manager.get_task_token(1)
    transports[0].emit_eof()
    await asyncio.sleep(0.05)
    await manager.ensure_container(
        task_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    token_b = manager.get_task_token(1)
    assert token_a and token_b and token_a != token_b, "令牌随容器重建轮换"


async def test_error_round_emits_error_and_done(tmp_path):
    history = [{"role": "user", "content": "x"}]
    manager, transports = make_manager(tmp_path, history)

    async def failing_make_runtime(spec, workdir_host, extension_path):
        transport = FakePiTransport()
        transport.fail_after_prompt = True
        transports.append(transport)

        async def noop() -> None:
            return None

        return transport, noop

    manager._make_runtime = failing_make_runtime  # type: ignore[method-assign]
    events, spy = await drive_round(manager, transports, "触发错误")
    names = [name for name, _ in events]
    assert "error" in names
    assert names[-1] == "done"
    assert spy.assistant == [], "error 回复不落库"
