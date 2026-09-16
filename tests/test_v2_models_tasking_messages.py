"""任务域模型：消息/事件/幂等/循环链编译面（Phase 9 T6 拆分）。

自 test_v2_models_tasking.py 后半逐字搬移（共享助手见 v2_tasking_model_helpers）。
"""

import uuid as _uuid

import pytest
from sqlalchemy import create_mock_engine, delete, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.v2.models import (
    EVENT_TYPES,
    MESSAGE_AUTHORS,
    Base,
    IdempotencyRecord,
    PlatformSlot,
    Task,
    TaskEvent,
    TaskMessage,
)
from tests.v2_tasking_model_helpers import (
    EXPIRES_SOON,
    _seed_task,
    _seed_task_parents,
    _task_kwargs,
)

pytestmark = [pytest.mark.usefixtures("pg_fresh")]  # 模型测试统一用 pg_fresh（自动 create_all）


async def test_task_message_unique_event_sequence(pg_fresh):
    """task_message_event_sequence 复合唯一：同 (task_id, event_sequence) → 违反；
    不同 seq 可建；author 封闭于 user/assistant/tool；created_at 由 server_default 填充。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert MESSAGE_AUTHORS == ("user", "assistant", "tool")
    task_id, parents = await _seed_task(maker, "msg@example.com")
    owner_id = parents["owner_id"]
    async with maker() as s:
        s.add(
            TaskMessage(
                task_id=task_id,
                owner_id=owner_id,
                event_sequence=0,
                author="user",
                content="第一问",
            )
        )
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(TaskMessage))).scalar_one()
        assert row.content == "第一问"
        assert row.created_at is not None
    async with maker() as s:
        s.add(
            TaskMessage(
                task_id=task_id,
                owner_id=owner_id,
                event_sequence=1,
                author="assistant",
                content="第一答",
            )
        )
        s.add(
            TaskMessage(
                task_id=task_id,
                owner_id=owner_id,
                event_sequence=2,
                author="tool",
                content="工具输出",
            )
        )
        await s.commit()
    # 同 (task_id, event_sequence) → 违反
    async with maker() as s:
        s.add(
            TaskMessage(
                task_id=task_id,
                owner_id=owner_id,
                event_sequence=1,
                author="user",
                content="重复序号",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # author 封闭枚举：system 被拒
    async with maker() as s:
        s.add(
            TaskMessage(
                task_id=task_id,
                owner_id=owner_id,
                event_sequence=3,
                author="system",
                content="x",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    async with pg_fresh.engine.connect() as conn:
        uqs = await conn.run_sync(lambda c: inspect(c).get_unique_constraints("task_messages"))
    uq = {u["name"]: u["column_names"] for u in uqs}
    assert uq["task_message_event_sequence"] == ["task_id", "event_sequence"]


async def test_task_event_unique_sequence_and_type_enum(pg_fresh):
    """task_event_sequence 复合唯一：同 (task_id, sequence) → 违反（另一 task 的
    同序号不受影响）；type 封闭于 EVENT_TYPES 7 值且逐一可插入；payload_json JSONB
    roundtrip；message_id/round_id 为裸 Uuid 弱引用（无 FK）。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert EVENT_TYPES == (
        "message_saved",
        "round_queued",
        "round_running",
        "round_settled",
        "round_failed",
        "round_cancelled",
        "status_changed",
    )
    task_id, parents = await _seed_task(maker, "events@example.com")
    owner_id = parents["owner_id"]
    async with maker() as s:
        for seq, etype in enumerate(EVENT_TYPES):
            s.add(
                TaskEvent(
                    task_id=task_id,
                    owner_id=owner_id,
                    sequence=seq,
                    type=etype,
                    payload_json={"seq": seq},
                )
            )
        await s.commit()
    async with maker() as s:
        rows = (await s.execute(select(TaskEvent).order_by(TaskEvent.sequence))).scalars().all()
        assert [r.type for r in rows] == list(EVENT_TYPES)
        assert rows[0].payload_json == {"seq": 0}
        assert rows[0].created_at is not None
    # 同 (task_id, sequence) → 违反
    async with maker() as s:
        s.add(
            TaskEvent(
                task_id=task_id,
                owner_id=owner_id,
                sequence=0,
                type="status_changed",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # type 封闭枚举：round_started 被拒
    async with maker() as s:
        s.add(
            TaskEvent(
                task_id=task_id,
                owner_id=owner_id,
                sequence=99,
                type="round_started",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # 另一 task 的 sequence=0 不受影响（复合唯一以 task 为界）
    task2_id, parents2 = await _seed_task(maker, "events2@example.com")
    async with maker() as s:
        s.add(
            TaskEvent(
                task_id=task2_id,
                owner_id=parents2["owner_id"],
                sequence=0,
                type="status_changed",
            )
        )
        await s.commit()
    async with pg_fresh.engine.connect() as conn:
        uqs = await conn.run_sync(lambda c: inspect(c).get_unique_constraints("task_events"))
    uq = {u["name"]: u["column_names"] for u in uqs}
    assert uq["task_event_sequence"] == ["task_id", "sequence"]


async def test_idempotency_unique_subject_route_key(pg_fresh):
    """idempotency_route_key 复合唯一：同 (subject_hash, route, key) → 违反；
    同 subject+route 换 key / 同 subject+key 换 route 均可建；expires_at NOT NULL；
    response_json / status_code 可空且可 roundtrip。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    subject = "s" * 64
    async with maker() as s:
        s.add(
            IdempotencyRecord(
                subject_hash=subject,
                route="/v1/tasks",
                key="idem-1",
                request_hash="r" * 64,
                expires_at=EXPIRES_SOON,
                response_json={"ok": True},
                status_code=201,
            )
        )
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(IdempotencyRecord))).scalar_one()
        assert row.response_json == {"ok": True}
        assert row.status_code == 201
        assert row.created_at is not None
    # 同三元组 → 违反
    async with maker() as s:
        s.add(
            IdempotencyRecord(
                subject_hash=subject,
                route="/v1/tasks",
                key="idem-1",
                request_hash="r" * 64,
                expires_at=EXPIRES_SOON,
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # 同 subject+route，不同 key → 合法
    async with maker() as s:
        s.add(
            IdempotencyRecord(
                subject_hash=subject,
                route="/v1/tasks",
                key="idem-2",
                request_hash="r" * 64,
                expires_at=EXPIRES_SOON,
            )
        )
        await s.commit()
    # 同 subject+key，不同 route → 合法
    async with maker() as s:
        s.add(
            IdempotencyRecord(
                subject_hash=subject,
                route="/v1/tasks/abort",
                key="idem-1",
                request_hash="r" * 64,
                expires_at=EXPIRES_SOON,
            )
        )
        await s.commit()
    # expires_at NOT NULL
    async with maker() as s:
        s.add(
            IdempotencyRecord(
                subject_hash=subject,
                route="/v1/x",
                key="idem-3",
                request_hash="r" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    async with pg_fresh.engine.connect() as conn:
        uqs = await conn.run_sync(
            lambda c: inspect(c).get_unique_constraints("idempotency_records")
        )
    uq = {u["name"]: u["column_names"] for u in uqs}
    assert uq["idempotency_route_key"] == ["subject_hash", "route", "key"]


async def test_task_initial_message_use_alter_fk(pg_fresh):
    """tasks.initial_message_id ↔ task_messages.task_id 互指（use_alter 循环 FK）：
    task + message 互指提交成功；FK 真实（指向不存在行 → IntegrityError）；
    ondelete SET NULL：删除初始消息 → 指针清空，task 保留。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    parents = await _seed_task_parents(maker, "initmsg@example.com")
    async with maker() as s:
        task = Task(**_task_kwargs(parents))
        s.add(task)
        await s.flush()
        msg = TaskMessage(
            task_id=task.id,
            owner_id=parents["owner_id"],
            event_sequence=0,
            author="user",
            content="初始输入",
        )
        s.add(msg)
        await s.flush()
        task.initial_message_id = msg.id
        await s.commit()
        task_id, msg_id = task.id, msg.id
    async with maker() as s:
        row = await s.get(Task, task_id)
        assert row.initial_message_id == msg_id
    # FK 真实：不存在的 message → IntegrityError
    async with maker() as s:
        s.add(Task(**_task_kwargs(parents, initial_message_id=_uuid.uuid4())))
        with pytest.raises(IntegrityError):
            await s.commit()
    # ondelete SET NULL：删除初始消息 → 指针清空
    async with maker() as s:
        await s.execute(delete(TaskMessage).where(TaskMessage.id == msg_id))
        await s.commit()
    async with maker() as s:
        row = await s.get(Task, task_id)
        assert row is not None
        assert row.initial_message_id is None


async def test_platform_slot_task_fk_set_null(pg_fresh):
    """platform_slots.task_id → tasks.id（Task 5 经 use_alter 接线）：
    指向不存在 task → IntegrityError；删除 task → 槽位 task_id 置 NULL（SET NULL）。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    task_id, _ = await _seed_task(maker, "slot@example.com")
    async with maker() as s:
        s.add(
            PlatformSlot(
                slot_no=10,
                state="leased",
                task_id=task_id,
                leased_until=EXPIRES_SOON,
            )
        )
        await s.commit()
    async with maker() as s:
        row = await s.get(PlatformSlot, 10)
        assert row.task_id == task_id
    async with maker() as s:
        s.add(PlatformSlot(slot_no=11, task_id=_uuid.uuid4()))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with maker() as s:
        await s.execute(delete(Task).where(Task.id == task_id))
        await s.commit()
    async with maker() as s:
        row = await s.get(PlatformSlot, 10)
        assert row is not None
        assert row.task_id is None


async def test_metadata_compile_circular_chain(pg_fresh):
    """元数据编译证明：循环链 tasks↔task_messages↔task_rounds↔platform_slots 在
    use_alter 下可拓扑排序（sorted_tables 不抛 CircularDependencyError），且
    postgresql 方言全量 DDL 中两个循环 FK 以 ALTER TABLE 补齐。"""
    order = [t.name for t in Base.metadata.sorted_tables]
    for name in ("tasks", "task_messages", "task_rounds", "platform_slots"):
        assert name in order

    dumped: list[str] = []
    engine = create_mock_engine(
        "postgresql://",
        lambda sql, *a, **kw: dumped.append(str(sql.compile(dialect=postgresql.dialect()))),
    )
    Base.metadata.create_all(engine, checkfirst=False)
    ddl = "\n".join(dumped)
    for table in (
        "tasks",
        "task_files",
        "task_reservations",
        "task_messages",
        "task_rounds",
        "task_events",
        "idempotency_records",
        "platform_slots",
    ):
        assert f"CREATE TABLE {table}" in ddl
    assert "ALTER TABLE tasks ADD CONSTRAINT fk_tasks_initial_message_id_task_messages" in ddl
    assert "ALTER TABLE platform_slots ADD CONSTRAINT fk_platform_slots_task_id_tasks" in ddl
