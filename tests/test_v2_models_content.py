"""内容与治理模型（9 张表）行为测试 — TDD Task 4。

与 Task 2/3 同模式：pg_fresh 夹具（create_all 建表）+ async_sessionmaker；
断言真实 PostgreSQL 约束行为（CHECK / 复合唯一 / use_alter 循环 FK /
ondelete SET NULL / 索引反射），不打桩。
"""

import uuid as _uuid

import pytest
from sqlalchemy import delete, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.v2.models import (
    ENTITY_STATUSES,
    REPORT_STATUSES,
    REVISION_STATUSES,
    AuditLog,
    ContentReview,
    Expert,
    ExpertRevision,
    Report,
    RevisionTool,
    Skill,
    SkillRevision,
    ToolCatalog,
    User,
)

pytestmark = [pytest.mark.usefixtures("pg_fresh")]  # 模型测试统一用 pg_fresh（自动 create_all）

SHA_A = "a" * 64
CONTENT = {"system_prompt": "You are a code review expert", "model": "gpt-4o"}


async def _seed_user(maker, email: str):
    async with maker() as s:
        user = User(email=email, password_hash="h", role="user", status="active")
        s.add(user)
        await s.commit()
        return user.id


async def _seed_expert(maker, email: str, status: str = "draft"):
    """建 user + expert，返回 (expert_id, owner_id)。"""
    async with maker() as s:
        user = User(email=email, password_hash="h", role="user", status="active")
        s.add(user)
        await s.flush()
        expert = Expert(owner_id=user.id, status=status)
        s.add(expert)
        await s.commit()
        return expert.id, user.id


def _revision_kwargs(expert_id, owner_id, **overrides):
    """ExpertRevision 构造基线；负例经 overrides 覆写单一字段。"""
    base = dict(
        expert_id=expert_id, owner_id=owner_id, revision_no=1,
        content_json=CONTENT, content_sha256=SHA_A,
    )
    base.update(overrides)
    return base


async def test_expert_revision_no_unique_per_expert(pg_fresh):
    """同 expert 两行 revision_no=1 → 违反复合唯一 expert_revision_no；
    不同 expert 复用 revision_no=1 不受影响（per-expert 而非全局）；
    owner_id 为插入时写入的冗余列（缺失即 NOT NULL 违例）；status 默认 draft。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    expert_id, owner_id = await _seed_expert(maker, "rev@example.com")
    other_expert_id, _ = await _seed_expert(maker, "rev-other@example.com")
    async with maker() as s:
        s.add(ExpertRevision(**_revision_kwargs(expert_id, owner_id)))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(ExpertRevision))).scalar_one()
        assert row.status == "draft"
        assert row.content_sha256 == SHA_A
    async with maker() as s:
        s.add(ExpertRevision(**_revision_kwargs(expert_id, owner_id)))
        with pytest.raises(IntegrityError):
            await s.commit()
    # revision_no 唯一性以 expert 为界：另一 expert 的 1 号版本合法
    async with maker() as s:
        s.add(ExpertRevision(**_revision_kwargs(other_expert_id, owner_id)))
        await s.commit()
    # 冗余 owner_id 缺失 → NOT NULL 违例
    kwargs = _revision_kwargs(expert_id, owner_id, revision_no=2)
    del kwargs["owner_id"]
    async with maker() as s:
        s.add(ExpertRevision(**kwargs))
        with pytest.raises(IntegrityError):
            await s.commit()
    # 反射：复合唯一约束名与列序与 DB §3 一字不差
    async with pg_fresh.engine.connect() as conn:
        uqs = await conn.run_sync(
            lambda c: inspect(c).get_unique_constraints("expert_revisions")
        )
    uq = {u["name"]: u["column_names"] for u in uqs}
    assert uq["expert_revision_no"] == ["expert_id", "revision_no"]


async def test_revision_status_enum_rejects_unknown(pg_fresh):
    """revision status 封闭于六值枚举：未知值 → IntegrityError；
    六个合法成员逐一可插入（同 expert 下 revision_no 各异互不冲突）。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    expert_id, owner_id = await _seed_expert(maker, "revstat@example.com")
    async with maker() as s:
        s.add(ExpertRevision(
            **_revision_kwargs(expert_id, owner_id, status="in_review"),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    assert REVISION_STATUSES == (
        "draft", "pending_review", "approved", "rejected", "published", "archived",
    )
    for no, status in enumerate(REVISION_STATUSES, start=1):
        async with maker() as s:
            s.add(ExpertRevision(
                **_revision_kwargs(expert_id, owner_id, revision_no=no, status=status),
            ))
            await s.commit()
    async with maker() as s:
        rows = (await s.execute(
            select(ExpertRevision).order_by(ExpertRevision.revision_no)
        )).scalars().all()
        assert [r.status for r in rows] == list(REVISION_STATUSES)


async def test_tool_catalog_unique_tool_id_version(pg_fresh):
    """同 (tool_id, version) 两行 → 违反 uq_tool_catalog_id_version；
    同 tool_id 不同 version 可再建（composite）；permissions JSONB roundtrip；
    enabled 默认 True。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    perms = {"fs": ["read", "write"]}
    async with maker() as s:
        s.add(ToolCatalog(tool_id="fs.write", version="1.0.0", permissions=perms))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(ToolCatalog))).scalar_one()
        assert row.permissions == {"fs": ["read", "write"]}
        assert row.enabled is True
    async with maker() as s:
        s.add(ToolCatalog(tool_id="fs.write", version="1.0.0", permissions=perms))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with maker() as s:
        s.add(ToolCatalog(tool_id="fs.write", version="1.1.0", permissions=perms))
        await s.commit()
    async with pg_fresh.engine.connect() as conn:
        uqs = await conn.run_sync(lambda c: inspect(c).get_unique_constraints("tool_catalog"))
    uq = {u["name"]: u["column_names"] for u in uqs}
    assert uq["uq_tool_catalog_id_version"] == ["tool_id", "version"]


async def test_revision_tool_rows_can_be_relisted(pg_fresh):
    """同一 revision 可列多个工具版本；完全相同三元组重复 → 违反 uq_revision_tools_row；
    删除一行后同一三元组可重新列入（relist）。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    expert_id, owner_id = await _seed_expert(maker, "tools@example.com")
    async with maker() as s:
        rev = ExpertRevision(**_revision_kwargs(expert_id, owner_id))
        s.add(rev)
        await s.flush()
        rev_id = rev.id
        s.add(RevisionTool(expert_revision_id=rev_id, tool_id="fs.write", version="1.0.0"))
        s.add(RevisionTool(expert_revision_id=rev_id, tool_id="net.fetch", version="2.3.1"))
        await s.commit()
    async with maker() as s:
        s.add(RevisionTool(expert_revision_id=rev_id, tool_id="fs.write", version="1.0.0"))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with maker() as s:
        row = (await s.execute(
            select(RevisionTool).where(
                RevisionTool.tool_id == "fs.write", RevisionTool.version == "1.0.0",
            )
        )).scalar_one()
        await s.delete(row)
        await s.commit()
    async with maker() as s:
        s.add(RevisionTool(expert_revision_id=rev_id, tool_id="fs.write", version="1.0.0"))
        await s.commit()  # relist 成功
        tools = (await s.execute(select(RevisionTool))).scalars().all()
        assert {(t.tool_id, t.version) for t in tools} == {
            ("fs.write", "1.0.0"), ("net.fetch", "2.3.1"),
        }


async def test_content_review_sha256_bound(pg_fresh):
    """审核记录绑定 revision hash（content_sha256 与被审 revision 一致存放）；
    target_type 封闭于 expert_revision/skill_revision（多态引用，不设硬 FK）；
    reviewer_id 可空（SET NULL 目标）；复合索引 ix_content_reviews_target_hash 真实存在。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    expert_id, owner_id = await _seed_expert(maker, "review@example.com")
    reviewer_id = await _seed_user(maker, "reviewer@example.com")
    async with maker() as s:
        rev = ExpertRevision(
            **_revision_kwargs(expert_id, owner_id, status="pending_review"),
        )
        s.add(rev)
        await s.flush()
        s.add(ContentReview(
            target_type="expert_revision", target_revision_id=rev.id,
            content_sha256=rev.content_sha256, result="approved",
            reviewer_id=reviewer_id,
        ))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(ContentReview))).scalar_one()
        assert row.content_sha256 == SHA_A
        assert row.result == "approved"
        assert row.reviewer_id == reviewer_id
        assert row.created_at is not None and row.updated_at is not None
    # 另一合法 target_type：skill_revision（指向尚不存在的行也可——多态不设硬 FK）
    async with maker() as s:
        s.add(ContentReview(
            target_type="skill_revision", target_revision_id=_uuid.uuid4(),
            content_sha256=SHA_A, result="rejected",
        ))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(
            select(ContentReview).where(ContentReview.target_type == "skill_revision")
        )).scalar_one()
        assert row.reviewer_id is None
    # target_type 封闭枚举：task 被拒
    async with maker() as s:
        s.add(ContentReview(
            target_type="task", target_revision_id=_uuid.uuid4(),
            content_sha256=SHA_A, result="approved",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with pg_fresh.engine.connect() as conn:
        indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("content_reviews"))
    idx = {i["name"]: i["column_names"] for i in indexes}
    assert idx["ix_content_reviews_target_hash"] == ["target_revision_id", "content_sha256"]


async def test_report_target_type_enum_and_reason(pg_fresh):
    """target_type 封闭于 expert_revision/skill_revision/message；status 封闭于
    open/dismissed/actioned（默认 open，三成员逐一可插入）；reason NOT NULL；
    created_at 由 server_default 填充。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    reporter_id = await _seed_user(maker, "reporter@example.com")
    assert REPORT_STATUSES == ("open", "dismissed", "actioned")
    async with maker() as s:
        s.add(Report(
            reporter_id=reporter_id, target_type="expert_revision",
            target_id=_uuid.uuid4(), reason="输出包含未经授权的内容",
        ))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(Report))).scalar_one()
        assert row.status == "open"
        assert row.created_at is not None
        assert row.target_revision_hash is None
    # 其余两个合法 status 成员逐一可插入；target_type 第三成员 message 同时覆盖
    for status in ("dismissed", "actioned"):
        async with maker() as s:
            s.add(Report(
                reporter_id=reporter_id, target_type="message",
                target_id=_uuid.uuid4(), reason="spam", status=status,
            ))
            await s.commit()
    # target_type 封闭枚举：user 被拒
    async with maker() as s:
        s.add(Report(
            reporter_id=reporter_id, target_type="user",
            target_id=_uuid.uuid4(), reason="x",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # status 封闭枚举：closed 被拒
    async with maker() as s:
        s.add(Report(
            reporter_id=reporter_id, target_type="expert_revision",
            target_id=_uuid.uuid4(), reason="x", status="closed",
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # reason NOT NULL
    async with maker() as s:
        s.add(Report(
            reporter_id=reporter_id, target_type="expert_revision",
            target_id=_uuid.uuid4(), reason=None,
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_audit_log_append_shape(pg_fresh):
    """append-only 形状：actor_id/action/target_type/target_id/reason/request_id/detail
    全字段 roundtrip；actor/target 可空（系统动作 / 解引用）；reason NOT NULL；
    created_at 由 server_default 填充且建索引。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    actor_id = await _seed_user(maker, "admin@example.com")
    target_id = _uuid.uuid4()
    detail = {"report": str(target_id), "verdict": "upheld"}
    async with maker() as s:
        s.add(AuditLog(
            actor_id=actor_id, action="report.actioned", target_type="report",
            target_id=target_id, reason="经查实违规", request_id="req-41f3",
            detail=detail,
        ))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(select(AuditLog))).scalar_one()
        assert row.action == "report.actioned"
        assert row.target_type == "report" and row.target_id == target_id
        assert row.reason == "经查实违规"
        assert row.request_id == "req-41f3"
        assert row.detail == detail
        assert row.created_at is not None
    # 系统动作：actor/target/request 均可空
    async with maker() as s:
        s.add(AuditLog(action="job.cleanup", target_type="system", reason="7 天保留期清理"))
        await s.commit()
    async with maker() as s:
        row = (await s.execute(
            select(AuditLog).where(AuditLog.action == "job.cleanup")
        )).scalar_one()
        assert row.actor_id is None and row.target_id is None and row.request_id is None
    # reason NOT NULL
    async with maker() as s:
        s.add(AuditLog(action="x", target_type="report", reason=None))
        with pytest.raises(IntegrityError):
            await s.commit()
    async with pg_fresh.engine.connect() as conn:
        indexes = await conn.run_sync(lambda c: inspect(c).get_indexes("audit_logs"))
    idx = {i["name"]: i["column_names"] for i in indexes}
    assert idx["ix_audit_logs_created_at"] == ["created_at"]


async def test_expert_published_revision_set_null_on_revision_delete(pg_fresh):
    """experts.published_revision_id → expert_revisions.id（use_alter 循环 FK）：
    删除被引用的 revision 行 → 指针被 ondelete SET NULL 清空，expert 行保留。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    expert_id, owner_id = await _seed_expert(maker, "publish@example.com")
    async with maker() as s:
        rev = ExpertRevision(
            **_revision_kwargs(expert_id, owner_id, status="published"),
        )
        s.add(rev)
        await s.flush()
        rev_id = rev.id
        expert = await s.get(Expert, expert_id)
        expert.published_revision_id = rev_id
        await s.commit()
    async with maker() as s:
        expert = await s.get(Expert, expert_id)
        assert expert.published_revision_id == rev_id
    async with maker() as s:
        await s.execute(delete(ExpertRevision).where(ExpertRevision.id == rev_id))
        await s.commit()
    async with maker() as s:
        expert = await s.get(Expert, expert_id)
        assert expert is not None
        assert expert.published_revision_id is None
        assert (await s.execute(select(ExpertRevision))).scalars().all() == []


async def test_skill_revision_mirror_and_set_null(pg_fresh):
    """skills/skill_revisions 与 experts/expert_revisions 同构：status 默认 draft、
    owner_id 冗余列、skill_revision_no 复合唯一（per-skill）、
    第二个 use_alter 循环 FK 的 SET NULL 行为、revision status 六值枚举共用。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    async with maker() as s:
        user = User(email="skill@example.com", password_hash="h", role="user", status="active")
        s.add(user)
        await s.flush()
        skill = Skill(owner_id=user.id)
        s.add(skill)
        await s.flush()
        skill_id, owner_id = skill.id, user.id
        s.add(SkillRevision(
            skill_id=skill_id, owner_id=owner_id, revision_no=1,
            content_json=CONTENT, content_sha256=SHA_A, status="published",
        ))
        await s.commit()
    async with maker() as s:
        assert (await s.get(Skill, skill_id)).status == "draft"
        rev = (await s.execute(select(SkillRevision))).scalar_one()
        skill = await s.get(Skill, skill_id)
        skill.published_revision_id = rev.id
        await s.commit()
        rev_id = rev.id
    # 同 skill 第二行 revision_no=1 被拒
    async with maker() as s:
        s.add(SkillRevision(
            skill_id=skill_id, owner_id=owner_id, revision_no=1,
            content_json=CONTENT, content_sha256=SHA_A,
        ))
        with pytest.raises(IntegrityError):
            await s.commit()
    # 删除被引用的 skill_revision → skill.published_revision_id 置空
    async with maker() as s:
        await s.execute(delete(SkillRevision).where(SkillRevision.id == rev_id))
        await s.commit()
    async with maker() as s:
        skill = await s.get(Skill, skill_id)
        assert skill.published_revision_id is None
    async with pg_fresh.engine.connect() as conn:
        uqs = await conn.run_sync(
            lambda c: inspect(c).get_unique_constraints("skill_revisions")
        )
    uq = {u["name"]: u["column_names"] for u in uqs}
    assert uq["skill_revision_no"] == ["skill_id", "revision_no"]


async def test_entity_status_enum_members(pg_fresh):
    """experts/skills.status 封闭于 ENTITY_STATUSES 三值（draft 默认）：
    三成员逐一可插入，未知值被拒。"""
    maker = async_sessionmaker(pg_fresh.engine, expire_on_commit=False)
    assert ENTITY_STATUSES == ("draft", "published", "archived")
    for i, status in enumerate(ENTITY_STATUSES):
        async with maker() as s:
            user = User(
                email=f"ent{i}@example.com", password_hash="h",
                role="user", status="active",
            )
            s.add(user)
            await s.flush()
            s.add(Expert(owner_id=user.id, status=status))
            await s.commit()
    async with maker() as s:
        statuses = (await s.execute(select(Expert.status))).scalars().all()
        assert sorted(statuses) == sorted(ENTITY_STATUSES)
    # 未知值被拒（owner 合法，仅 status 违例）
    async with maker() as s:
        user = User(email="entbad@example.com", password_hash="h", role="user", status="active")
        s.add(user)
        await s.flush()
        s.add(Expert(owner_id=user.id, status="retired"))
        with pytest.raises(IntegrityError):
            await s.commit()
    # skill 共用同一封闭枚举
    async with maker() as s:
        user = User(
            email="skillstat@example.com", password_hash="h",
            role="user", status="active",
        )
        s.add(user)
        await s.flush()
        s.add(Skill(owner_id=user.id, status="published"))
        await s.commit()
    async with maker() as s:
        user = User(email="skillbad@example.com", password_hash="h", role="user", status="active")
        s.add(user)
        await s.flush()
        s.add(Skill(owner_id=user.id, status="hidden"))
        with pytest.raises(IntegrityError):
            await s.commit()
