"""任务域模型（7 张表）行为测试 — TDD Task 5。

与 Task 2/3/4 同模式：pg_fresh 夹具（create_all 建表）+ async_sessionmaker；
断言真实 PostgreSQL 约束行为（CHECK / 部分唯一索引 / 复合唯一 / use_alter
互指 FK / ondelete / 索引反射），不打桩。索引与约束名与 DB §3/§5 一字不差；
use_alter 循环链（tasks↔task_messages↔task_rounds↔platform_slots）另以
metadata 全量 DDL 编译证明（postgresql 方言）。
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_mock_engine, delete, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.v2.models import (
    EVENT_TYPES,
    FILE_STATES,
    MESSAGE_AUTHORS,
    RESERVATION_KINDS,
    RESERVATION_STATES,
    ROUND_STATES,
    TASK_STATUSES,
    Base,
    Expert,
    ExpertRevision,
    IdempotencyRecord,
    PlatformSlot,
    ProviderCatalog,
    Task,
    TaskEvent,
    TaskFile,
    TaskMessage,
    TaskReservation,
    TaskRound,
    User,
    UserProvider,
)

pytestmark = [pytest.mark.usefixtures("pg_fresh")]  # 模型测试统一用 pg_fresh（自动 create_all）

SHA_A = "a" * 64
EXPIRES_SOON = datetime.now(timezone.utc) + timedelta(hours=1)
CONTENT = {"system_prompt": "s", "model": "gpt-4o"}


async def _seed_task_parents(maker, email: str):
    """建 task 全部硬 FK 父行：user + expert + expert_revision + catalog +
    user_provider，返回构造 Task 所需的 id 字典。"""
    async with maker() as s:
        user = User(email=email, password_hash="h", role="user", status="active")
        s.add(user)
        await s.flush()
        expert = Expert(owner_id=user.id, status="published")
        s.add(expert)
        await s.flush()
        rev = ExpertRevision(
            expert_id=expert.id, owner_id=user.id, revision_no=1,
            content_json=CONTENT, content_sha256=SHA_A, status="published",
        )
        s.add(rev)
        catalog = ProviderCatalog(
            display_name="OpenAI", allowed_host="api.openai.com",
            models=["gpt-4o"], healthcheck_path="/v1/models",
        )
        s.add(catalog)
        await s.flush()
        provider = UserProvider(
            user_id=user.id, catalog_id=catalog.id, model_id="gpt-4o",
            key_ciphertext="ct", dek_wrapped="dek", key_last4="Ab1!",
        )
        s.add(provider)
        await s.commit()
        return {
            "owner_id": user.id,
            "expert_revision_id": rev.id,
            "provider_id": provider.id,
        }


def _task_kwargs(parents, **overrides):
    """Task 构造基线；负例经 overrides 覆写单一字段。"""
    base = dict(
        owner_id=parents["owner_id"],
        expert_revision_id=parents["expert_revision_id"],
        provider_id=parents["provider_id"],
        provider_catalog_id=_uuid.uuid4(),  # 快照列：裸 Uuid，无 FK
        provider_model_id="gpt-4o",
        provider_key_version=1,
    )
    base.update(overrides)
    return base


async def _seed_task(maker, email: str, **task_overrides):
    """建完整父链 + task，返回 (task_id, parents)。"""
    parents = await _seed_task_parents(maker, email)
    async with maker() as s:
        task = Task(**_task_kwargs(parents, **task_overrides))
        s.add(task)
        await s.commit()
        return task.id, parents


async def _seed_message(maker, task_id, owner_id, event_sequence=0):
    """建一条 user 消息，返回 message id（event_sequence 由调用方保证 per-task 唯一）。"""
    async with maker() as s:
        msg = TaskMessage(
            task_id=task_id, owner_id=owner_id, event_sequence=event_sequence,
            author="user", content="初始输入",
        )
        s.add(msg)
        await s.commit()
        return msg.id


async def _seed_task_with_message(maker, email: str):
    task_id, parents = await _seed_task(maker, email)
    msg_id = await _seed_message(maker, task_id, parents["owner_id"])
    return task_id, parents, msg_id


def _file_kwargs(task_id, owner_id, file_name="report.pdf", **overrides):
    """TaskFile 构造基线；负例经 overrides 覆写单一字段。"""
    base = dict(
        task_id=task_id, owner_id=owner_id, direction="input",
        file_name=file_name,
        storage_key=f"tasks/{task_id}/{_uuid.uuid4()}",
        sha256=_uuid.uuid4().hex * 2,  # 64 hex
        size_bytes=1024,
    )
    base.update(overrides)
    return base


async def test_task_status_enum_eight_states(pg_fresh):
    """status 封闭于 8 值枚举：'creating' 已裁决移除 → 必须被拒；'ready' 接受；
    默认 uploading 且 event_sequence 默认 0；8 成员逐一可插入；
    deferred RESTRICT FK 真实生效（被 task 引用的 expert_revision 不可删）。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert TASK_STATUSES == (
        "uploading", "queued", "running", "ready", "completed", "failed", "aborted", "deleted",
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
                delete(ExpertRevision).where(
                    ExpertRevision.id == parents["expert_revision_id"]
                )
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
    cancelled 不占活跃名额（谓词范围正对照）；首轮转 settled 后新 pending 可建。"""
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
        s.add(TaskRound(
            task_id=task_id, owner_id=owner_id, source_message_id=msg2, state="running",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # cancelled 不在谓词内：active 轮存活期间可存在 cancelled 轮（正对照）
    async with maker() as s:
        s.add(TaskRound(
            task_id=task_id, owner_id=owner_id, source_message_id=msg2, state="cancelled",
        ))
        await s.commit()
    # 首轮转 settled → 退出谓词 → 新 pending 可建（msg3：msg2 已被 cancelled 轮占用）
    async with maker() as s:
        row = await s.get(TaskRound, round1_id)
        row.state = "settled"
        await s.commit()
    msg3 = await _seed_message(maker, task_id, owner_id, event_sequence=2)
    async with maker() as s:
        s.add(TaskRound(task_id=task_id, owner_id=owner_id, source_message_id=msg3))
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
        s.add(TaskRound(
            task_id=task_id, owner_id=owner_id, source_message_id=msg1, state="settled",
        ))
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
    kind/state 封闭枚举；user/kind/state 组合索引反射。"""
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
        s.add(TaskReservation(
            task_id=task_id, user_id=owner_id, kind="active", state="consumed",
        ))
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
    # kind / state 封闭枚举
    async with maker() as s:
        s.add(TaskReservation(task_id=task_id, user_id=owner_id, kind="stale"))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with maker() as s:
        s.add(TaskReservation(
            task_id=task_id, user_id=owner_id, kind="active", state="expired",
        ))
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
        "user_id", "kind", "state",
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
        s.add(TaskReservation(
            task_id=task_id, user_id=owner_id, kind="artifact_copy", file_id=f1_id, bytes=10,
        ))
        s.add(TaskReservation(
            task_id=task_id, user_id=owner_id, kind="artifact_copy", file_id=f2_id, bytes=10,
        ))
        await s.commit()
    # 同 file 第二行 → 违反
    async with maker() as s:
        s.add(TaskReservation(
            task_id=task_id, user_id=owner_id, kind="artifact_copy", file_id=f1_id,
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # f1 预留 released → 同 file 可再建
    async with maker() as s:
        row = (await s.execute(
            select(TaskReservation).where(
                TaskReservation.kind == "artifact_copy", TaskReservation.file_id == f1_id,
            )
        )).scalar_one()
        row.state = "released"
        await s.commit()
    async with maker() as s:
        s.add(TaskReservation(
            task_id=task_id, user_id=owner_id, kind="artifact_copy", file_id=f1_id,
        ))
        await s.commit()
    # file_id FK 真实：指向不存在 file → IntegrityError
    async with maker() as s:
        s.add(TaskReservation(
            task_id=task_id, user_id=owner_id, kind="artifact_copy", file_id=_uuid.uuid4(),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # ondelete CASCADE：删除 file2 → 其预留行级联消失
    async with maker() as s:
        await s.execute(delete(TaskFile).where(TaskFile.id == f2_id))
        await s.commit()
    async with maker() as s:
        remaining = (await s.execute(
            select(TaskReservation).where(TaskReservation.file_id == f2_id)
        )).scalars().all()
        assert remaining == []


async def test_task_file_name_single_segment_and_states(pg_fresh):
    """file_name 禁路径分隔（CHECK file_name_single_segment：'/' 与 '\\' 字面量
    均被拒）；direction 封闭于 input/output；state 封闭于 FILE_STATES（默认 staged）；
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
    # '\\' 字面量被拒（CHECK 文本 position('\\' ...) 按标准字符串匹配连续反斜杠）
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
            s.add(TaskFile(
                **_file_kwargs(task_id, owner_id, file_name=f"f{i}.pdf",
                               direction="output", state=state),
            ))
        await s.commit()
    # 冗余 owner_id 缺失 → NOT NULL 违例
    kwargs = _file_kwargs(task_id, owner_id, file_name="g.pdf")
    del kwargs["owner_id"]
    async with maker() as s:
        s.add(TaskFile(**kwargs))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_task_message_unique_event_sequence(pg_fresh):
    """task_message_event_sequence 复合唯一：同 (task_id, event_sequence) → 违反；
    不同 seq 可建；author 封闭于 user/assistant/tool；created_at 由 server_default 填充。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert MESSAGE_AUTHORS == ("user", "assistant", "tool")
    task_id, parents = await _seed_task(maker, "msg@example.com")
    owner_id = parents["owner_id"]
    async with maker() as s:
        s.add(TaskMessage(
            task_id=task_id, owner_id=owner_id, event_sequence=0,
            author="user", content="第一问",
        ))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(TaskMessage))).scalar_one()
        assert row.content == "第一问"
        assert row.created_at is not None
    async with maker() as s:
        s.add(TaskMessage(
            task_id=task_id, owner_id=owner_id, event_sequence=1,
            author="assistant", content="第一答",
        ))
        s.add(TaskMessage(
            task_id=task_id, owner_id=owner_id, event_sequence=2,
            author="tool", content="工具输出",
        ))
        await s.commit()
    # 同 (task_id, event_sequence) → 违反
    async with maker() as s:
        s.add(TaskMessage(
            task_id=task_id, owner_id=owner_id, event_sequence=1,
            author="user", content="重复序号",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # author 封闭枚举：system 被拒
    async with maker() as s:
        s.add(TaskMessage(
            task_id=task_id, owner_id=owner_id, event_sequence=3,
            author="system", content="x",
        ))
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
        "message_saved", "round_queued", "round_running", "round_settled",
        "round_failed", "round_cancelled", "status_changed",
    )
    task_id, parents = await _seed_task(maker, "events@example.com")
    owner_id = parents["owner_id"]
    async with maker() as s:
        for seq, etype in enumerate(EVENT_TYPES):
            s.add(TaskEvent(
                task_id=task_id, owner_id=owner_id, sequence=seq, type=etype,
                payload_json={"seq": seq},
            ))
        await s.commit()
    async with maker() as s:
        rows = (await s.execute(
            select(TaskEvent).order_by(TaskEvent.sequence)
        )).scalars().all()
        assert [r.type for r in rows] == list(EVENT_TYPES)
        assert rows[0].payload_json == {"seq": 0}
        assert rows[0].created_at is not None
    # 同 (task_id, sequence) → 违反
    async with maker() as s:
        s.add(TaskEvent(
            task_id=task_id, owner_id=owner_id, sequence=0, type="status_changed",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # type 封闭枚举：round_started 被拒
    async with maker() as s:
        s.add(TaskEvent(
            task_id=task_id, owner_id=owner_id, sequence=99, type="round_started",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # 另一 task 的 sequence=0 不受影响（复合唯一以 task 为界）
    task2_id, parents2 = await _seed_task(maker, "events2@example.com")
    async with maker() as s:
        s.add(TaskEvent(
            task_id=task2_id, owner_id=parents2["owner_id"], sequence=0,
            type="status_changed",
        ))
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
        s.add(IdempotencyRecord(
            subject_hash=subject, route="/v1/tasks", key="idem-1",
            request_hash="r" * 64, expires_at=EXPIRES_SOON,
            response_json={"ok": True}, status_code=201,
        ))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(IdempotencyRecord))).scalar_one()
        assert row.response_json == {"ok": True}
        assert row.status_code == 201
        assert row.created_at is not None
    # 同三元组 → 违反
    async with maker() as s:
        s.add(IdempotencyRecord(
            subject_hash=subject, route="/v1/tasks", key="idem-1",
            request_hash="r" * 64, expires_at=EXPIRES_SOON,
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # 同 subject+route，不同 key → 合法
    async with maker() as s:
        s.add(IdempotencyRecord(
            subject_hash=subject, route="/v1/tasks", key="idem-2",
            request_hash="r" * 64, expires_at=EXPIRES_SOON,
        ))
        await s.commit()
    # 同 subject+key，不同 route → 合法
    async with maker() as s:
        s.add(IdempotencyRecord(
            subject_hash=subject, route="/v1/tasks/abort", key="idem-1",
            request_hash="r" * 64, expires_at=EXPIRES_SOON,
        ))
        await s.commit()
    # expires_at NOT NULL
    async with maker() as s:
        s.add(IdempotencyRecord(
            subject_hash=subject, route="/v1/x", key="idem-3", request_hash="r" * 64,
        ))
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
            task_id=task.id, owner_id=parents["owner_id"],
            event_sequence=0, author="user", content="初始输入",
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
        s.add(PlatformSlot(
            slot_no=10, state="leased", task_id=task_id, leased_until=EXPIRES_SOON,
        ))
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
        "tasks", "task_files", "task_reservations", "task_messages",
        "task_rounds", "task_events", "idempotency_records", "platform_slots",
    ):
        assert f"CREATE TABLE {table}" in ddl
    assert "ALTER TABLE tasks ADD CONSTRAINT fk_tasks_initial_message_id_task_messages" in ddl
    assert "ALTER TABLE platform_slots ADD CONSTRAINT fk_platform_slots_task_id_tasks" in ddl
