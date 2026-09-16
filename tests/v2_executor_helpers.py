"""执行器测试共享助手（Phase 9 T6 拆分自 test_v2_task_executor.py）。

rt 夹具 + _make_executor/_scalar/_rows/_run_round/_wait_prompt/_drain_round_tasks/
_sync_event_watermark——被 test_v2_task_executor（轮执行/围栏/续约/僵尸对账面）与
test_v2_task_executor_termination（T6b 终止面）共同消费。
"""

import asyncio
import base64

import pytest
from sqlalchemy import text

from backend.v2.task_executor import (
    RoundExecutor,
)
from backend.v2.task_storage import TaskStorage
from backend.v2.task_streams import TaskStreamRegistry
from tests.conftest import FakePiTransport
from tests.test_v2_runtime import make_v2_runtime

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
