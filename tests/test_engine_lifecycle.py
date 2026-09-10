"""引擎生命周期测试（手册 §7.2 并发上限/空闲回收、§6.6 queued 事件）。

- 并发上限：容器槽位 semaphore（PI_MAX_CONCURRENT_CONTAINERS）；槽满时新
  发送先收到 SSE queued 事件，槽释放后自动继续
- 空闲回收：连续 PI_IDLE_TIMEOUT_MINUTES 无活动且无活动轮 → 回收容器；
  回收后下一条消息自动重建并重播种
"""

import asyncio
from pathlib import Path

import pytest

from tests.conftest import FakePiTransport, make_scripted_manager

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def env(tmp_path: Path):
    manager, transports = make_scripted_manager(tmp_path, round_timeout=30, max_concurrent=1)
    return manager, transports


async def test_second_task_gets_queued_when_slot_full(env):
    """槽位被任务 A 占用 → 任务 B 的 run_round 先 yield queued。"""
    manager, transports = env

    async def empty_history(task_id, limit):
        return []

    async def persist_assistant(content, usage):
        return {"message_id": 1}

    async def persist_tool(call_id, name, content, is_error):
        return {"message_id": 2}

    gen_a = manager.run_round(
        task_id=1,
        user_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot={},
        task_files=[],
        expert_name="e",
        content="a",
        persist_assistant=persist_assistant,
        persist_tool=persist_tool,
    )
    first_a = await gen_a.__anext__()
    assert first_a[0] != "queued"  # 槽空闲：A 直接开跑
    async for _ in gen_a:  # 消费至 done
        pass

    # A 结束但容器仍在池中（槽被容器占用）
    assert 1 in manager._containers

    gen_b = manager.run_round(
        task_id=2,
        user_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot={},
        task_files=[],
        expert_name="e",
        content="b",
        persist_assistant=persist_assistant,
        persist_tool=persist_tool,
    )
    first_b = await gen_b.__anext__()
    assert first_b == ("queued", {}), "槽满时新任务必须先收到 queued"

    # 释放 A 的容器（空闲回收路径）→ B 自动继续到 done
    await manager.stop_container(1)
    events = [event async for event in gen_b]
    assert events[-1][0] == "done"


async def test_slot_released_on_teardown(env):
    manager, _transports = env
    await manager.ensure_container(
        task_id=1,
        user_id=1,
        provider_config_id=None,
        stored_workdir="/workspaces/authorized",
        skill_snapshot={},
        task_files=[],
        expert_name="e",
    )
    assert manager._slots.locked()
    await manager.stop_container(1)
    assert not manager._slots.locked(), "容器回收必须释放槽位"


async def test_idle_sweep_reclaims_and_next_message_reseeds(env):
    manager, _transports = env
    await manager.ensure_container(
        task_id=1,
        user_id=1,
        provider_config_id=None,
        stored_workdir="/workspaces/authorized",
        skill_snapshot={},
        task_files=[],
        expert_name="e",
    )
    engine_before = manager._containers[1]
    # 回溯活动时间到远超空闲阈值
    manager._last_activity[1] -= manager._settings.PI_IDLE_TIMEOUT_MINUTES * 60 + 1
    await manager.sweep_idle()
    assert 1 not in manager._containers, "超时无活动容器应被回收"
    assert not manager._slots.locked()

    # 回收后下一条消息：重建 + 重播种
    async def empty_history(task_id, limit):
        return []

    async def noop(content, usage):
        return {"message_id": 1}

    gen = manager.run_round(
        task_id=1,
        user_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot={},
        task_files=[],
        expert_name="e",
        content="again",
        persist_assistant=noop,
        persist_tool=noop,
    )
    async for _ in gen:
        pass
    engine_after = manager._containers[1]
    assert engine_after is not engine_before
    assert engine_after.needs_reseed is False, "本轮已重播种"


async def test_sweep_keeps_active_container(env):
    manager, _transports = env
    await manager.ensure_container(
        task_id=1,
        user_id=1,
        provider_config_id=None,
        stored_workdir="/workspaces/authorized",
        skill_snapshot={},
        task_files=[],
        expert_name="e",
    )
    manager._last_activity[1] -= manager._settings.PI_IDLE_TIMEOUT_MINUTES * 60 + 1
    engine = manager._containers[1]
    engine.is_round_settled = False  # 活动轮进行中
    await manager.sweep_idle()
    assert 1 in manager._containers, "活动轮进行中不得回收"


# ---------------------------------------------------------------------------
# 崩溃恢复（§7.8：重建 + 重播种，最多 3 次）
# ---------------------------------------------------------------------------


class CrashFactory:
    """前 N 个 transport 在首个 prompt 后 EOF（模拟容器崩溃）。"""

    def __init__(self, crash_count: int) -> None:
        self.remaining = crash_count


async def test_crash_recovery_rebuilds_and_reseeds(tmp_path):
    manager, transports = make_scripted_manager(tmp_path, round_timeout=30)
    crashes = CrashFactory(1)

    async def fake_make_runtime(spec, workdir_host, extension_path):
        transport = FakePiTransport()
        if crashes.remaining > 0:
            transport.crash_on_prompts = 1
            crashes.remaining -= 1
        transports.append(transport)

        async def noop() -> None:
            return None

        return transport, noop

    manager._make_runtime = fake_make_runtime  # type: ignore[method-assign]

    async def noop(content, usage):
        return {"message_id": 1}

    events = [
        event
        async for event in manager.run_round(
            task_id=1,
            user_id=1,
            stored_workdir="/workspaces/authorized",
            skill_snapshot={},
            task_files=[],
            expert_name="e",
            content="retry",
            persist_assistant=noop,
            persist_tool=noop,
        )
    ]
    assert len(transports) == 2, "崩溃后必须重建容器"
    assert events[-1][0] == "done"
    assert manager._containers[1].needs_reseed is False
    second_prompts = [w for w in transports[1].written if w.get("type") == "prompt"]
    assert second_prompts and second_prompts[0]["message"] == "retry"


async def test_crash_recovery_exhausts_and_marks_failed(tmp_path):
    manager, transports = make_scripted_manager(tmp_path, round_timeout=30)
    marked: list[int] = []

    async def mark_failed(task_id: int) -> None:
        marked.append(task_id)

    manager._mark_task_failed = mark_failed

    async def fake_make_runtime(spec, workdir_host, extension_path):
        transport = FakePiTransport()
        transport.crash_on_prompts = 1
        transports.append(transport)

        async def noop() -> None:
            return None

        return transport, noop

    manager._make_runtime = fake_make_runtime  # type: ignore[method-assign]

    async def noop(content, usage):
        return {"message_id": 1}

    events = [
        event
        async for event in manager.run_round(
            task_id=1,
            user_id=1,
            stored_workdir="/workspaces/authorized",
            skill_snapshot={},
            task_files=[],
            expert_name="e",
            content="retry",
            persist_assistant=noop,
            persist_tool=noop,
        )
    ]
    assert len(transports) == 3, "最多重建 3 次"
    errors = [payload for name, payload in events if name == "error"]
    assert errors and errors[0]["code"] == "ENGINE_CRASHED"
    assert marked == [1], "恢复耗尽必须标记 failed（可重试）"
    assert events[-1][0] == "done"


# ---------------------------------------------------------------------------
# 看门狗（§7.8.1：容器死亡 → failed；任务总超时 → abort+failed）
# ---------------------------------------------------------------------------


async def _make_container(manager, task_id: int = 1):
    await manager.ensure_container(
        task_id=task_id,
        user_id=1,
        provider_config_id=None,
        stored_workdir="/workspaces/authorized",
        skill_snapshot={},
        task_files=[],
        expert_name="e",
    )
    return manager._containers[task_id]


async def test_watchdog_reclaims_dead_container_and_marks_failed(tmp_path):
    from datetime import datetime, timezone

    manager, _transports = make_scripted_manager(tmp_path, round_timeout=30)
    engine = await _make_container(manager)
    engine._transport.emit_eof()  # 模拟容器崩溃（stdout EOF）
    await asyncio.sleep(0.05)
    assert engine._reader_task.done()

    marked: list[int] = []

    async def mark_failed(task_id: int) -> None:
        marked.append(task_id)

    manager._mark_task_failed = mark_failed

    async def fetch_running():
        return [{"id": 1, "running_since": datetime.now(timezone.utc)}]

    manager._running_tasks_fetcher = fetch_running
    reclaimed = await manager.sweep_watchdog()
    assert reclaimed == 1
    assert marked == [1]
    assert 1 not in manager._containers


async def test_watchdog_total_timeout_aborts_active_round(tmp_path):
    from datetime import datetime, timedelta, timezone

    manager, _transports = make_scripted_manager(tmp_path, round_timeout=30, max_lifetime_minutes=1)
    engine = await _make_container(manager)
    engine.is_round_settled = False  # 活动轮进行中

    marked: list[int] = []

    async def mark_failed(task_id: int) -> None:
        marked.append(task_id)

    manager._mark_task_failed = mark_failed

    async def fetch_running():
        return [
            {
                "id": 1,
                "running_since": datetime.now(timezone.utc) - timedelta(minutes=5),
            }
        ]

    manager._running_tasks_fetcher = fetch_running
    reclaimed = await manager.sweep_watchdog()
    assert reclaimed == 1
    assert engine._transport.aborted, "总超时必须先 abort 活动轮"
    assert marked == [1]
    assert 1 not in manager._containers, "总超时后回收容器"


async def test_watchdog_ignores_healthy_recent_task(tmp_path):
    from datetime import datetime, timezone

    manager, _transports = make_scripted_manager(tmp_path, round_timeout=30)
    await _make_container(manager)
    marked: list[int] = []

    async def mark_failed(task_id: int) -> None:
        marked.append(task_id)

    manager._mark_task_failed = mark_failed

    async def fetch_running():
        return [{"id": 1, "running_since": datetime.now(timezone.utc)}]

    manager._running_tasks_fetcher = fetch_running
    assert await manager.sweep_watchdog() == 0
    assert marked == []
    assert 1 in manager._containers
