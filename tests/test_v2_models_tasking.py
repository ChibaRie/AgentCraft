"""任务域模型（7 张表）行为测试 — 约束面（Phase 9 T6 拆分：原 TDD Task 5 文件一分为二）。

与 Task 2/3/4 同模式：pg_fresh 夹具（create_all 建表）+ async_sessionmaker；
断言真实 PostgreSQL 约束行为（CHECK / 部分唯一索引 / 复合唯一 / ondelete /
索引反射），不打桩。索引与约束名与 DB §3/§5 一字不差。共享助手见
v2_tasking_model_helpers；消息/事件/幂等/循环链编译面见
test_v2_models_tasking_messages.py。
"""

import uuid as _uuid

import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.v2.models import (
    FILE_STATES,
    RESERVATION_KINDS,
    RESERVATION_STATES,
    ROUND_STATES,
    TASK_STATUSES,
    ExpertRevision,
    Task,
    TaskFile,
    TaskMessage,
    TaskReservation,
    TaskRound,
)
from tests.v2_tasking_model_helpers import (
    _file_kwargs,
    _seed_message,
    _seed_task,
    _seed_task_parents,
    _seed_task_with_message,
    _task_kwargs,
)

pytestmark = [pytest.mark.usefixtures("pg_fresh")]  # 模型测试统一用 pg_fresh（自动 create_all）


async def test_task_status_enum_eight_states(pg_fresh):
    """status 封闭于 8 值枚举：'creating' 已裁决移除 → 必须被拒；'ready' 接受；
    默认 uploading 且 event_sequence 默认 0；8 成员逐一可插入；
    deferred RESTRICT FK 真实生效（被 task 引用的 expert_revision 不可删）。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert TASK_STATUSES == (
        "uploading",
        "queued",
        "running",
        "ready",
        "completed",
        "failed",
        "aborted",
        "deleted",
    )
    parents = await _seed_task_parents(maker, "taskstat@example.com")
    async with maker() as s:
        task = Task(**_task_kwargs(parents))
        s.add(task)
        await s.commit()
        task_id = task.id
    async with maker() as s:
        row = await s.get(Task, task_id)
        assert row.status == "uploading"
        assert row.event_sequence == 0
    # 'ready' 接受
    async with maker() as s:
        row = await s.get(Task, task_id)
        row.status = "ready"
        await s.commit()
    # 'creating' 被拒（裁决移除）
    async with maker() as s:
        s.add(Task(**_task_kwargs(parents, status="creating")))
        with pytest.raises(IntegrityError):
            await s.commit()
    # 8 个合法成员逐一可插入（首行已转 ready，故 8 行全新插入）
    for status in TASK_STATUSES:
        async with maker() as s:
            s.add(Task(**_task_kwargs(parents, status=status)))
            await s.commit()
    async with maker() as s:
        statuses = (await s.execute(select(Task.status))).scalars().all()
        assert set(statuses) == set(TASK_STATUSES)
    # RESTRICT：被 task 引用的 expert_revision 不可删（bulk DELETE 在 execute 即报 FK）
    async with maker() as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                delete(ExpertRevision).where(ExpertRevision.id == parents["expert_revision_id"])
            )
    # 反射：tasks 三索引名与列序与 DB §5 一字不差（ix_tasks_queued 为 partial）
    async with pg_fresh.engine.connect() as conn:
        indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("tasks"))
    idx = {i["name"]: i for i in indexes}
    assert idx["ix_tasks_owner_created"]["column_names"] == ["owner_id", "created_at"]
    assert idx["ix_tasks_id_owner"]["column_names"] == ["id", "owner_id"]
    assert idx["ix_tasks_queued"]["column_names"] == ["status", "created_at"]
    assert "queued" in str(idx["ix_tasks_queued"]["dialect_options"]["postgresql_where"])


async def test_one_active_round_per_task_partial_index(pg_fresh):
    """部分唯一索引 one_active_round_per_task（谓词 state IN
    pending/running/cancelling）：同 task 两行 active 态 → 违反；
    cancelled 不占活跃名额（谓词范围正对照）；首轮转 settled 后新 pending 可建；
    另覆盖 ROUND_STATES 的 cancelling/failed 正面插入与未知 state 拒绝（F2a）。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert ROUND_STATES == ("pending", "running", "cancelling", "settled", "failed", "cancelled")
    task_id, parents, msg1 = await _seed_task_with_message(maker, "round@example.com")
    owner_id = parents["owner_id"]
    async with maker() as s:
        r1 = TaskRound(task_id=task_id, owner_id=owner_id, source_message_id=msg1)
        s.add(r1)
        await s.commit()
        round1_id = r1.id
        assert r1.state == "pending"
    msg2 = await _seed_message(maker, task_id, owner_id, event_sequence=1)
    # 第二条 pending（不同 source_message）→ 违反 one_active_round_per_task
    async with maker() as s:
        s.add(TaskRound(task_id=task_id, owner_id=owner_id, source_message_id=msg2))
        with pytest.raises(IntegrityError):
            await s.commit()
    # 谓词覆盖 running：pending + running 同样违反
    async with maker() as s:
        s.add(
            TaskRound(
                task_id=task_id,
                owner_id=owner_id,
                source_message_id=msg2,
                state="running",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # cancelled 不在谓词内：active 轮存活期间可存在 cancelled 轮（正对照）
    async with maker() as s:
        s.add(
            TaskRound(
                task_id=task_id,
                owner_id=owner_id,
                source_message_id=msg2,
                state="cancelled",
            )
        )
        await s.commit()
    # 首轮转 settled → 退出谓词 → 新 pending 可建（msg3：msg2 已被 cancelled 轮占用）
    async with maker() as s:
        row = await s.get(TaskRound, round1_id)
        row.state = "settled"
        await s.commit()
    msg3 = await _seed_message(maker, task_id, owner_id, event_sequence=2)
    async with maker() as s:
        r3 = TaskRound(task_id=task_id, owner_id=owner_id, source_message_id=msg3)
        s.add(r3)
        await s.commit()
        round3_id = r3.id
    # F2(a)：cancelling 正面插入（msg3 轮让位 active 名额后）
    async with maker() as s:
        row = await s.get(TaskRound, round3_id)
        row.state = "settled"
        await s.commit()
    msg4 = await _seed_message(maker, task_id, owner_id, event_sequence=3)
    async with maker() as s:
        s.add(
            TaskRound(
                task_id=task_id,
                owner_id=owner_id,
                source_message_id=msg4,
                state="cancelling",
            )
        )
        await s.commit()
    # F2(a)：failed 正面插入（failed 不在谓词内，与存活 cancelling 轮并存合法）
    msg5 = await _seed_message(maker, task_id, owner_id, event_sequence=4)
    async with maker() as s:
        s.add(
            TaskRound(
                task_id=task_id,
                owner_id=owner_id,
                source_message_id=msg5,
                state="failed",
            )
        )
        await s.commit()
    # F2(a)：未知 state 被拒（ck_task_rounds_state_enum）
    msg6 = await _seed_message(maker, task_id, owner_id, event_sequence=5)
    async with maker() as s:
        s.add(
            TaskRound(
                task_id=task_id,
                owner_id=owner_id,
                source_message_id=msg6,
                state="settling",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # 反射：partial 索引名 + 列 + 唯一 + 谓词
    async with pg_fresh.engine.connect() as conn:
        indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("task_rounds"))
    idx = {i["name"]: i for i in indexes}
    assert idx["one_active_round_per_task"]["column_names"] == ["task_id"]
    assert idx["one_active_round_per_task"]["unique"] is True
    where = str(idx["one_active_round_per_task"]["dialect_options"]["postgresql_where"])
    assert "pending" in where and "running" in where and "cancelling" in where


async def test_round_source_message_unique(pg_fresh):
    """round_source_message 唯一不分区状态：同 source_message_id 两轮
    （即便首轮已 settled）→ 违反；不同 source_message 可建；
    source_message_id → task_messages 为 RESTRICT：被轮引用的消息不可删。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    task_id, parents, msg1 = await _seed_task_with_message(maker, "srcmsg@example.com")
    owner_id = parents["owner_id"]
    async with maker() as s:
        s.add(
            TaskRound(
                task_id=task_id,
                owner_id=owner_id,
                source_message_id=msg1,
                state="settled",
            )
        )
        await s.commit()
    # 首轮已 settled（非活跃）但唯一约束不分状态 → 仍违反
    async with maker() as s:
        s.add(TaskRound(task_id=task_id, owner_id=owner_id, source_message_id=msg1))
        with pytest.raises(IntegrityError):
            await s.commit()
    msg2 = await _seed_message(maker, task_id, owner_id, event_sequence=1)
    async with maker() as s:
        s.add(TaskRound(task_id=task_id, owner_id=owner_id, source_message_id=msg2))
        await s.commit()
    # RESTRICT：被轮引用的 message 不可删（bulk DELETE 在 execute 即报 FK）
    async with maker() as s:
        with pytest.raises(IntegrityError):
            await s.execute(delete(TaskMessage).where(TaskMessage.id == msg1))
    async with pg_fresh.engine.connect() as conn:
        uqs = await conn.run_sync(lambda c: inspect(c).get_unique_constraints("task_rounds"))
    uq = {u["name"]: u["column_names"] for u in uqs}
    assert uq["round_source_message"] == ["source_message_id"]


async def test_reservation_unique_active_per_task(pg_fresh):
    """one_live_active_reservation：同 task 两行 kind=active 活动态（held/consumed）
    → 违反；released 退出谓词 → 可再建；kind 之间互不影响（task_root 与 active 并存）；
    kind/state 封闭枚举；kind=running 与 active 同构证明（F2b）；
    user/kind/state 组合索引反射。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert RESERVATION_KINDS == ("active", "running", "task_root", "artifact_copy")
    assert RESERVATION_STATES == ("held", "consumed", "released")
    task_id, parents = await _seed_task(maker, "reserve@example.com")
    owner_id = parents["owner_id"]
    async with maker() as s:
        r1 = TaskReservation(task_id=task_id, user_id=owner_id, kind="active", bytes=1024)
        s.add(r1)
        await s.commit()
        r1_id = r1.id
        assert r1.state == "held"
    # 第二行 active(held) → 违反
    async with maker() as s:
        s.add(TaskReservation(task_id=task_id, user_id=owner_id, kind="active"))
        with pytest.raises(IntegrityError):
            await s.commit()
    # consumed 也在谓词内：active(consumed) 与 active(held) 并存被拒
    async with maker() as s:
        s.add(
            TaskReservation(
                task_id=task_id,
                user_id=owner_id,
                kind="active",
                state="consumed",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # kind 互不影响：task_root 与 active 并存合法（不同 partial 索引）
    async with maker() as s:
        s.add(TaskReservation(task_id=task_id, user_id=owner_id, kind="task_root"))
        await s.commit()
    # r1 released → 退出谓词 → 新 active 可建
    async with maker() as s:
        row = await s.get(TaskReservation, r1_id)
        row.state = "released"
        await s.commit()
    async with maker() as s:
        s.add(TaskReservation(task_id=task_id, user_id=owner_id, kind="active"))
        await s.commit()
    # F2(b)：kind="running" 与 active 同构——one_live_running_reservation
    async with maker() as s:
        rr = TaskReservation(task_id=task_id, user_id=owner_id, kind="running", bytes=64)
        s.add(rr)
        await s.commit()
        running1_id = rr.id
    async with maker() as s:
        s.add(TaskReservation(task_id=task_id, user_id=owner_id, kind="running"))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with maker() as s:
        row = await s.get(TaskReservation, running1_id)
        row.state = "released"
        await s.commit()
    async with maker() as s:
        s.add(TaskReservation(task_id=task_id, user_id=owner_id, kind="running"))
        await s.commit()
    # kind / state 封闭枚举
    async with maker() as s:
        s.add(TaskReservation(task_id=task_id, user_id=owner_id, kind="stale"))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with maker() as s:
        s.add(
            TaskReservation(
                task_id=task_id,
                user_id=owner_id,
                kind="active",
                state="expired",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # 反射：三个 per-task partial 唯一索引 + artifact_copy 复合列 + 组合索引
    async with pg_fresh.engine.connect() as conn:
        indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("task_reservations"))
    idx = {i["name"]: i for i in indexes}
    for name, kind in (
        ("one_live_active_reservation", "active"),
        ("one_live_running_reservation", "running"),
        ("one_live_task_root_reservation", "task_root"),
    ):
        assert idx[name]["column_names"] == ["task_id"]
        assert idx[name]["unique"] is True
        # PG 反射会改写谓词文本（IN → = ANY(ARRAY[...])），断言关键 token 而非原文
        where = str(idx[name]["dialect_options"]["postgresql_where"])
        assert f"'{kind}'" in where and "'held'" in where and "'consumed'" in where
    artifact = idx["one_live_artifact_copy_reservation"]
    assert artifact["column_names"] == ["task_id", "file_id"] and artifact["unique"] is True
    assert "'artifact_copy'" in str(artifact["dialect_options"]["postgresql_where"])
    assert idx["ix_task_reservations_user_kind_state"]["column_names"] == [
        "user_id",
        "kind",
        "state",
    ]


async def test_artifact_copy_reservation_per_file(pg_fresh):
    """one_live_artifact_copy_reservation（(task_id, file_id) 谓词 kind=artifact_copy
    AND state IN held/consumed）：同 task 两个不同 file 的 artifact_copy 并存合法；
    同 file 两行拒绝；released 后同 file 可再建；file_id FK 真实且 ondelete CASCADE。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    task_id, parents = await _seed_task(maker, "copy@example.com")
    owner_id = parents["owner_id"]
    async with maker() as s:
        f1 = TaskFile(**_file_kwargs(task_id, owner_id, file_name="a.pdf"))
        f2 = TaskFile(**_file_kwargs(task_id, owner_id, file_name="b.pdf"))
        s.add_all([f1, f2])
        await s.commit()
        f1_id, f2_id = f1.id, f2.id
    # 两个不同 file 的 artifact_copy(held) 同时允许
    async with maker() as s:
        s.add(
            TaskReservation(
                task_id=task_id,
                user_id=owner_id,
                kind="artifact_copy",
                file_id=f1_id,
                bytes=10,
            )
        )
        s.add(
            TaskReservation(
                task_id=task_id,
                user_id=owner_id,
                kind="artifact_copy",
                file_id=f2_id,
                bytes=10,
            )
        )
        await s.commit()
    # 同 file 第二行 → 违反
    async with maker() as s:
        s.add(
            TaskReservation(
                task_id=task_id,
                user_id=owner_id,
                kind="artifact_copy",
                file_id=f1_id,
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # f1 预留 released → 同 file 可再建
    async with maker() as s:
        row = (
            await s.execute(
                select(TaskReservation).where(
                    TaskReservation.kind == "artifact_copy",
                    TaskReservation.file_id == f1_id,
                )
            )
        ).scalar_one()
        row.state = "released"
        await s.commit()
    async with maker() as s:
        s.add(
            TaskReservation(
                task_id=task_id,
                user_id=owner_id,
                kind="artifact_copy",
                file_id=f1_id,
            )
        )
        await s.commit()
    # file_id FK 真实：指向不存在 file → IntegrityError
    async with maker() as s:
        s.add(
            TaskReservation(
                task_id=task_id,
                user_id=owner_id,
                kind="artifact_copy",
                file_id=_uuid.uuid4(),
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()
    # ondelete CASCADE：删除 file2 → 其预留行级联消失
    async with maker() as s:
        await s.execute(delete(TaskFile).where(TaskFile.id == f2_id))
        await s.commit()
    async with maker() as s:
        remaining = (
            (await s.execute(select(TaskReservation).where(TaskReservation.file_id == f2_id)))
            .scalars()
            .all()
        )
        assert remaining == []


async def test_task_file_name_single_segment_and_states(pg_fresh):
    """file_name 禁路径分隔（CHECK file_name_single_segment：'/' 与任意反斜杠
    均被拒——渲染 SQL 的 needle 为单字符 '\\'，Windows 分隔符 a\\b.pdf 无法绕过）；
    direction 封闭于 input/output；state 封闭于 FILE_STATES（默认 staged）；
    冗余 owner_id 缺失 → NOT NULL 违例。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert FILE_STATES == ("staged", "committed", "registered", "deleted")
    task_id, parents = await _seed_task(maker, "files@example.com")
    owner_id = parents["owner_id"]
    async with maker() as s:
        s.add(TaskFile(**_file_kwargs(task_id, owner_id)))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(TaskFile))).scalar_one()
        assert row.state == "staged"
        assert row.storage_key.startswith(f"tasks/{task_id}")
    # '/' 被拒
    async with maker() as s:
        s.add(TaskFile(**_file_kwargs(task_id, owner_id, file_name="a/b.pdf")))
        with pytest.raises(IntegrityError):
            await s.commit()
    # 单个反斜杠被拒（Windows 分隔符 a\b.pdf —— F1：needle 必须是单字符 '\'）
    async with maker() as s:
        s.add(TaskFile(**_file_kwargs(task_id, owner_id, file_name="a\\b.pdf")))
        with pytest.raises(IntegrityError):
            await s.commit()
    # 连续双反斜杠同样被拒（含单反斜段子串）
    async with maker() as s:
        s.add(TaskFile(**_file_kwargs(task_id, owner_id, file_name="a\\\\b.pdf")))
        with pytest.raises(IntegrityError):
            await s.commit()
    # direction 封闭枚举：io 被拒
    async with maker() as s:
        s.add(TaskFile(**_file_kwargs(task_id, owner_id, direction="io")))
        with pytest.raises(IntegrityError):
            await s.commit()
    # output 方向合法；FILE_STATES 其余成员可插入
    async with maker() as s:
        for i, state in enumerate(FILE_STATES[1:], start=1):
            s.add(
                TaskFile(
                    **_file_kwargs(
                        task_id, owner_id, file_name=f"f{i}.pdf", direction="output", state=state
                    ),
                )
            )
        await s.commit()
    # 冗余 owner_id 缺失 → NOT NULL 违例
    kwargs = _file_kwargs(task_id, owner_id, file_name="g.pdf")
    del kwargs["owner_id"]
    async with maker() as s:
        s.add(TaskFile(**kwargs))
        with pytest.raises(IntegrityError):
            await s.commit()
