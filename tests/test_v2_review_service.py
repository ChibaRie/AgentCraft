"""review_service 测试（admin 引擎；D14 锁序/断言序/CAS；D16 错误码消费）。"""

import uuid as _uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.errors import AgentCraftError, ErrorCode
from backend.v2 import review_service
from backend.v2.content_hash import content_sha256
from backend.v2.models.content import ExpertRevision
from tests.v2_content_helpers import (
    EXPERT_CONTENT,
    seed_entity_with_revision,
    seed_revision_tools,
)
from tests.v2_provider_helpers import seed_active_user

_SEQ = {"n": 0}  # 每测试独立克隆库 → 从 1 起的确定性邮箱即可保证唯一


def _content() -> dict:
    return dict(EXPERT_CONTENT)


async def _seed_pending(pg, tools=(), content=None, sha=None) -> tuple[str, str, str]:
    """作者 + pending_review revision（+tools），返回 (author_id, entity_id, revision_id)。"""
    _SEQ["n"] += 1
    author = await seed_active_user(pg, f"author-{_SEQ['n']}@x.com")
    content = content if content is not None else _content()
    sha = sha or content_sha256(content)
    entity_id, revision_id = await seed_entity_with_revision(
        pg,
        author,
        "experts",
        entity_status="draft",
        revision_status="pending_review",
        content_json=content,
        content_sha256=sha,
    )
    if tools:
        await seed_revision_tools(pg, revision_id, tools)
    return author, entity_id, revision_id


async def _approve(pg, rt, target_type, revision_id, *, reason="质量合格", request_id="req-1"):
    """reviewer 是真实 users 行（ContentReview.reviewer_id / AuditLog.actor_id 有
    FK users）——伪 UUID 会 IntegrityError。每测试独立库，种一个 reviewer 即可。"""
    reviewer = await seed_active_user(pg, f"reviewer-{_SEQ['n']}@x.com")
    async with rt.admin_factory() as db:
        async with db.begin():
            return await review_service.approve_revision(
                db,
                target_type=target_type,
                revision_id=revision_id,
                reviewer_id=reviewer,
                reason=reason,
                request_id=request_id,
            )


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_publishes_atomically(pg, provider_env):
    _, entity_id, revision_id = await _seed_pending(
        pg, tools=[("check_code_style", "1"), ("read_task_file", "1")]
    )
    out = await _approve(pg, provider_env, "expert_revision", revision_id)
    assert out["entity_status"] == "published"  # D4：实体翻转
    assert out["published_revision_id"] == revision_id  # CAS 指针
    assert out["previous_published_revision_id"] is None
    async with pg.engine.begin() as conn:  # superuser 复核全链状态
        rev_status = (
            await conn.execute(
                text("SELECT status FROM expert_revisions WHERE id = CAST(:r AS uuid)"),
                {"r": revision_id},
            )
        ).scalar_one()
        assert rev_status == "published"
        review = (
            await conn.execute(
                text(
                    "SELECT result, content_sha256 FROM content_reviews "
                    "WHERE target_revision_id = CAST(:r AS uuid)"
                ),
                {"r": revision_id},
            )
        ).one()
        assert review.result == "approved"
        audit = (
            await conn.execute(
                text("SELECT count(*) FROM audit_logs WHERE action = 'expert.approve_publish'")
            )
        ).scalar_one()
        assert audit == 1


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_archives_previous_published(pg, provider_env):
    author = await seed_active_user(pg, "repub@x.com")
    content1 = _content()
    content2 = dict(content1, persona="第二版 persona。")
    e1, r1 = await seed_entity_with_revision(
        pg,
        author,
        "experts",
        entity_status="published",
        revision_status="published",
        content_json=content1,
        content_sha256=content_sha256(content1),
        with_pointer=True,
    )
    _, r2 = await seed_entity_with_revision(  # 同实体第二条 revision：复用 e1，no=2
        pg,
        author,
        "experts",
        entity_id=e1,
        revision_no=2,
        revision_status="pending_review",
        content_json=content2,
        content_sha256=content_sha256(content2),
    )
    out = await _approve(pg, provider_env, "expert_revision", r2)
    assert out["previous_published_revision_id"] == r1
    async with pg.engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT status FROM expert_revisions WHERE id = CAST(:r AS uuid)"),
                {"r": r1},
            )
        ).scalar_one() == "archived"  # 旧公开 revision 归档
        assert (
            await conn.execute(
                text("SELECT published_revision_id FROM experts WHERE id = CAST(:e AS uuid)"),
                {"e": e1},
            )
        ).scalar_one() == _uuid.UUID(r2)


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_requires_reason(pg, provider_env):
    _, _, revision_id = await _seed_pending(pg)
    with pytest.raises(AgentCraftError) as excinfo:
        await _approve(pg, provider_env, "expert_revision", revision_id, reason="   ")
    assert excinfo.value.code == ErrorCode.ADMIN_REASON_REQUIRED
    assert excinfo.value.http_status == 400


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_non_pending_409(pg, provider_env):
    author = await seed_active_user(pg, "draftcase@x.com")
    _, revision_id = await seed_entity_with_revision(
        pg, author, "experts", revision_status="draft"
    )  # 未提审
    with pytest.raises(AgentCraftError) as excinfo:
        await _approve(pg, provider_env, "expert_revision", revision_id)
    assert excinfo.value.code == ErrorCode.REVIEW_PENDING


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_hash_drift_409(pg, provider_env):
    # TOCTOU 模拟：superuser 改 content_json 但保留旧 hash → 三重比对失败
    _, _, revision_id = await _seed_pending(pg)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE expert_revisions SET content_json = CAST(:c AS jsonb) "
                "WHERE id = CAST(:r AS uuid)"
            ),
            {"c": '{"tampered": true}', "r": revision_id},
        )
    with pytest.raises(AgentCraftError) as excinfo:
        await _approve(pg, provider_env, "expert_revision", revision_id)
    assert excinfo.value.code == ErrorCode.REVIEW_PENDING


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_disabled_tool_409(pg, provider_env):
    _, _, revision_id = await _seed_pending(pg, tools=[("check_code_style", "1")])
    async with pg.engine.begin() as conn:  # kill switch：目录行禁用
        await conn.execute(
            text("UPDATE tool_catalog SET enabled = false WHERE tool_id = 'check_code_style'")
        )
    with pytest.raises(AgentCraftError) as excinfo:
        await _approve(pg, provider_env, "expert_revision", revision_id)
    assert excinfo.value.code == ErrorCode.TOOL_REVOKED


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_unpublished_skill_ref_409(pg, provider_env):
    _SEQ["n"] += 1
    skill_owner = await seed_active_user(pg, f"skillowner-{_SEQ['n']}@x.com")
    _, draft_skill_rev = await seed_entity_with_revision(  # 他人 draft skill revision
        pg, skill_owner, "skills", revision_status="draft"
    )
    async with pg.engine.begin() as conn:  # skill_id 须与 revision 真实配对
        real_skill_id = str(
            (
                await conn.execute(
                    text("SELECT skill_id FROM skill_revisions WHERE id = CAST(:r AS uuid)"),
                    {"r": draft_skill_rev},
                )
            ).scalar_one()
        )
    content = _content()
    content["skill_refs"] = [{"skill_id": real_skill_id, "revision_id": draft_skill_rev}]
    _, _, revision_id = await _seed_pending(pg, content=content, sha=content_sha256(content))
    with pytest.raises(AgentCraftError) as excinfo:
        await _approve(pg, provider_env, "expert_revision", revision_id)
    assert excinfo.value.code == ErrorCode.REVISION_NOT_PUBLISHED


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_reject_flow(pg, provider_env):
    _, entity_id, revision_id = await _seed_pending(pg)
    _SEQ["n"] += 1
    reviewer = await seed_active_user(pg, f"rejector-{_SEQ['n']}@x.com")
    async with provider_env.admin_factory() as db:
        async with db.begin():
            out = await review_service.reject_revision(
                db,
                target_type="expert_revision",
                revision_id=revision_id,
                reviewer_id=reviewer,
                reason="内容不实",
                request_id="req-2",
            )
    assert out["status"] == "rejected"
    async with pg.engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT status FROM expert_revisions WHERE id = CAST(:r AS uuid)"),
                {"r": revision_id},
            )
        ).scalar_one() == "rejected"
        assert (
            await conn.execute(
                text(
                    "SELECT result FROM content_reviews WHERE target_revision_id = CAST(:r AS uuid)"
                ),
                {"r": revision_id},
            )
        ).scalar_one() == "rejected"
        # 实体与指针未被 reject 触碰
        row = (
            await conn.execute(
                text(
                    "SELECT status, published_revision_id FROM experts WHERE id = CAST(:e AS uuid)"
                ),
                {"e": entity_id},
            )
        ).one()
        assert row.status == "draft" and row.published_revision_id is None


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_rollback_leaves_no_trace(pg, provider_env):
    """I1 强化「写入后失败」路径：CAS + revision UPDATE 已执行/入队后，最终 flush
    因 reviewer FK（users 无此行）违例 → 整事务回滚。superuser 复核：实体指针/status
    未变、revision 仍 pending_review、content_reviews/audit_logs 零行——count==0
    得以区分「回滚了」与「从未写过」（旧版 TOOL_REVOKED 在全部写之前抛出，断言空转）。"""
    _, entity_id, revision_id = await _seed_pending(pg, tools=[("check_code_style", "1")])
    ghost = str(_uuid.uuid4())  # 格式合法但 users 无此行 → flush 时 FK 违例
    async with provider_env.admin_factory() as db:
        with pytest.raises(IntegrityError):
            async with db.begin():
                await review_service.approve_revision(
                    db,
                    target_type="expert_revision",
                    revision_id=revision_id,
                    reviewer_id=ghost,
                    reason="质量合格",
                    request_id="req-1",
                )
    async with pg.engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, published_revision_id FROM experts WHERE id = CAST(:e AS uuid)"
                ),
                {"e": entity_id},
            )
        ).one()
        assert row.status == "draft" and row.published_revision_id is None
        assert (
            await conn.execute(
                text("SELECT status FROM expert_revisions WHERE id = CAST(:r AS uuid)"),
                {"r": revision_id},
            )
        ).scalar_one() == "pending_review"
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM content_reviews "
                    "WHERE target_revision_id = CAST(:r AS uuid)"
                ),
                {"r": revision_id},
            )
        ).scalar_one() == 0
        assert (await conn.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one() == 0


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_reread_after_lock_sees_concurrent_reject(pg, provider_env):
    """C1 回归（identity map 击穿锁后重读）：A 无锁首读（污染 identity map）→
    B reject 同一 revision 并提交 → A 同事务同会话继续 approve。交错用顺序 await
    天然确定（无需等锁）。锁后重读必须看到 rejected（populate_existing 覆盖锁前
    快照）→ 409 REVIEW_PENDING；superuser 复核 revision 仍 rejected、content_reviews
    无 approved 行（未修复时已拒 revision 会被发布且双 result 并存）。"""
    _SEQ["n"] += 1
    reviewer = await seed_active_user(pg, f"racer-{_SEQ['n']}@x.com")
    _, _, revision_id = await _seed_pending(pg)
    async with provider_env.admin_factory() as db_a:
        with pytest.raises(AgentCraftError) as excinfo:
            async with db_a.begin():
                # A 锁前快照（approve 内部首读同款无锁语句）→ 污染 A 的 identity map
                stale = (
                    await db_a.execute(
                        select(ExpertRevision).where(ExpertRevision.id == _uuid.UUID(revision_id))
                    )
                ).scalar_one()
                assert stale.status == "pending_review"
                # B 独立 admin 会话完整 reject 并提交（A 仅持 MVCC 快照，无锁，不阻塞 B）
                async with provider_env.admin_factory() as db_b:
                    async with db_b.begin():
                        await review_service.reject_revision(
                            db_b,
                            target_type="expert_revision",
                            revision_id=revision_id,
                            reviewer_id=reviewer,
                            reason="并发拒绝",
                            request_id="req-c1",
                        )
                # A 同一事务/会话继续 approve：若锁后重读仍取 identity map 锁前
                # 旧实例（status=pending_review），已拒 revision 将被发布
                await review_service.approve_revision(
                    db_a,
                    target_type="expert_revision",
                    revision_id=revision_id,
                    reviewer_id=reviewer,
                    reason="质量合格",
                    request_id="req-c1",
                )
        assert excinfo.value.code == ErrorCode.REVIEW_PENDING
        assert excinfo.value.http_status == 409
    async with pg.engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT status FROM expert_revisions WHERE id = CAST(:r AS uuid)"),
                {"r": revision_id},
            )
        ).scalar_one() == "rejected"
        results = (
            (
                await conn.execute(
                    text(
                        "SELECT result FROM content_reviews "
                        "WHERE target_revision_id = CAST(:r AS uuid)"
                    ),
                    {"r": revision_id},
                )
            )
            .scalars()
            .all()
        )
        assert results == ["rejected"]


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_skill_revision_no_tool_assertion(pg, provider_env):
    # skill 域：无 tools/skill_refs 断言面，直接发布
    author = await seed_active_user(pg, "skillpub@x.com")
    content = {
        "name": "技能",
        "description": "一个用于测试的技能描述。",
        "use_case": "测试场景使用。",
        "role": "测试者",
        "goal": "验证技能发布链路可用。",
        "steps": "提交并审批。",
        "input_requirements": None,
        "output_requirements": "发布成功的结果。",
        "constraints": "仅用于测试。",
    }
    _, revision_id = await seed_entity_with_revision(
        pg,
        author,
        "skills",
        entity_status="draft",
        revision_status="pending_review",
        content_json=content,
        content_sha256=content_sha256(content),
    )
    out = await _approve(pg, provider_env, "skill_revision", revision_id)
    assert out["entity_status"] == "published"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_rejects_cross_owner_revision(pg, provider_env):
    """D14 交叉校验：revision.owner_id 与实体 owner 不一致（应用层 bug/审核队列
    投毒形态——revisions INSERT policy 只查 owner_id，expert_id FK 不绑 owner）
    → 409 REVIEW_PENDING，绝不发布。"""
    _SEQ["n"] += 1
    author = await seed_active_user(pg, f"poison-a-{_SEQ['n']}@x.com")
    intruder = await seed_active_user(pg, f"poison-b-{_SEQ['n']}@x.com")
    entity_id, _ = await seed_entity_with_revision(pg, author, "experts")
    _, poisoned_rev = await seed_entity_with_revision(  # 他人 owner 的 revision 挂到 author 实体
        pg,
        intruder,
        "experts",
        entity_id=entity_id,
        revision_status="pending_review",
        revision_no=2,
    )
    with pytest.raises(AgentCraftError) as excinfo:
        await _approve(pg, provider_env, "expert_revision", poisoned_rev)
    assert excinfo.value.code == ErrorCode.REVIEW_PENDING
    assert excinfo.value.http_status == 409


# ---------- T4 重构回归：_reason_gate 收敛至 backend.v2.reason_gate（行为不变）----------


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_approve_reason_stripped_into_audit(pg, provider_env):
    """reason 门 strip 语义经共享 require_reason 保持：带首尾空白的合法 reason 照常
    过门，审计 reason 为 strip 后值（重构前后行为一致——12 例既有零变化之外的行为钉）。"""
    _, _, revision_id = await _seed_pending(pg)
    out = await _approve(pg, provider_env, "expert_revision", revision_id, reason="  两端空白  ")
    assert out["entity_status"] == "published"
    async with pg.engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT reason FROM audit_logs WHERE action = 'expert.approve_publish'")
            )
        ).scalar_one() == "两端空白"


def test_require_reason_shared_gate_behavior():
    """require_reason 单元行为（=原 review_service._reason_gate 逐字迁移）：
    None/非 str/空白 → AgentCraftError(ADMIN_REASON_REQUIRED, 400)；合法值 strip 返回。"""
    from backend.v2.reason_gate import require_reason

    with pytest.raises(AgentCraftError) as excinfo:
        require_reason(None)
    assert excinfo.value.code == ErrorCode.ADMIN_REASON_REQUIRED
    assert excinfo.value.http_status == 400
    with pytest.raises(AgentCraftError) as excinfo:
        require_reason(123)  # type: ignore[arg-type]
    assert excinfo.value.code == ErrorCode.ADMIN_REASON_REQUIRED
    with pytest.raises(AgentCraftError) as excinfo:
        require_reason("   \t")
    assert excinfo.value.code == ErrorCode.ADMIN_REASON_REQUIRED
    assert require_reason("  合规理由  ") == "合规理由"
