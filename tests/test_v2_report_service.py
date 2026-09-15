"""report_service 测试（D10 目标矩阵 / D15 takedown / D16 新码）。"""

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.errors import AgentCraftError, ErrorCode
from backend.v2 import idempotency, report_service
from backend.v2.content_hash import content_sha256
from backend.v2.ids import uuid7
from tests.v2_content_helpers import (
    EXPERT_CONTENT,
    seed_entitlement,
    seed_entity_with_revision,
)
from tests.v2_provider_helpers import (
    seed_active_user,
    seed_provider,
    seed_task_for_provider,
)


async def _create_report(rt, reporter_id: str, payload: dict, key: str = "r1"):
    return await report_service.create_report(
        rt,
        reporter_id=reporter_id,
        payload=payload,
        idem_key=key,
        idem_hash=idempotency.request_hash(payload),
    )


_SEQ = {"n": 0}


async def _seed_admin(pg) -> str:
    """真实 admin 用户（AuditLog.actor_id FK users——伪 UUID 会 IntegrityError）。"""
    _SEQ["n"] += 1
    return await seed_active_user(pg, f"admin-{_SEQ['n']}@x.com")


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_report_published_revision_ok(pg, provider_env):
    author = await seed_active_user(pg, "rp-author@x.com")
    reporter = await seed_active_user(pg, "rp-reporter@x.com")
    _, revision_id = await seed_entity_with_revision(
        pg,
        author,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    payload = {"target_type": "expert_revision", "target_id": revision_id, "reason": "内容违规"}
    out = await _create_report(provider_env, reporter, payload)
    assert out["status"] == "open"
    assert out["target_id"] == revision_id


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_report_missing_target_404(pg, provider_env):
    reporter = await seed_active_user(pg, "rp-miss@x.com")
    payload = {"target_type": "expert_revision", "target_id": str(uuid7()), "reason": "x"}
    with pytest.raises(HTTPException) as excinfo:
        await _create_report(provider_env, reporter, payload)
    assert excinfo.value.status_code == 404  # D10：不存在 → 统一 404


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_report_own_draft_400_invalid_target(pg, provider_env):
    reporter = await seed_active_user(pg, "rp-draft@x.com")
    _, revision_id = await seed_entity_with_revision(
        pg, reporter, "experts", revision_status="draft"
    )  # 自己的 draft 可见
    payload = {"target_type": "expert_revision", "target_id": revision_id, "reason": "x"}
    with pytest.raises(HTTPException) as excinfo:
        await _create_report(provider_env, reporter, payload)
    assert excinfo.value.status_code == 400  # D10：可见但非公开 → REPORT_INVALID_TARGET
    assert excinfo.value.detail["code"] == "REPORT_INVALID_TARGET"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_report_message_requires_assistant_author(pg, provider_env):
    reporter = await seed_active_user(pg, "rp-msg@x.com")
    provider_id = await seed_provider(pg, reporter)  # 见 helper；仅为造任务链
    task_id = await seed_task_for_provider(pg, reporter, provider_id)
    async with pg.engine.begin() as conn:
        assistant_msg = (
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), CAST(:t AS uuid), "
                    "CAST(:u AS uuid), 99, 'assistant', 'AI 回复') RETURNING id"
                ),
                {"t": task_id, "u": reporter},
            )
        ).scalar_one()
        user_msg = (
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), CAST(:t AS uuid), "
                    "CAST(:u AS uuid), 100, 'user', '用户消息') RETURNING id"
                ),
                {"t": task_id, "u": reporter},
            )
        ).scalar_one()
    with pytest.raises(HTTPException) as excinfo:  # user 消息不可举报（D11）
        await _create_report(
            provider_env,
            reporter,
            {"target_type": "message", "target_id": str(user_msg), "reason": "x"},
        )
    assert excinfo.value.status_code == 400
    out = await _create_report(
        provider_env,
        reporter,
        {"target_type": "message", "target_id": str(assistant_msg), "reason": "AI 输出有害"},
    )
    assert out["status"] == "open"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_resolve_dismiss(pg, provider_env):
    reporter = await seed_active_user(pg, "rp-dism@x.com")
    admin = await _seed_admin(pg)
    # dismiss 测试不经过目标校验：直接 superuser 造 open report
    async with pg.engine.begin() as conn:
        report_id = (
            await conn.execute(
                text(
                    "INSERT INTO reports (id, reporter_id, target_type, target_id, status, reason) "
                    "VALUES (gen_random_uuid(), CAST(:u AS uuid), 'expert_revision', "
                    "gen_random_uuid(), 'open', '测试') RETURNING id"
                ),  # status NOT NULL 且无 server_default（0001:361，ORM default 不作用于裸 SQL）
                {"u": reporter},
            )
        ).scalar_one()
    async with provider_env.admin_factory() as db:
        async with db.begin():
            out = await report_service.resolve_report(
                db,
                report_id=str(report_id),
                action="dismiss",
                admin_id=admin,
                reason="证据不足",
                request_id="req-3",
            )
    assert out["status"] == "dismissed"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_resolve_twice_409(pg, provider_env):
    reporter = await seed_active_user(pg, "rp-twice@x.com")
    admin = await _seed_admin(pg)
    async with pg.engine.begin() as conn:
        report_id = (
            await conn.execute(
                text(
                    "INSERT INTO reports (id, reporter_id, target_type, target_id, status, reason) "
                    "VALUES (gen_random_uuid(), CAST(:u AS uuid), 'expert_revision', "
                    "gen_random_uuid(), 'open', '测试') RETURNING id"
                ),  # status NOT NULL 且无 server_default（0001:361，ORM default 不作用于裸 SQL）
                {"u": reporter},
            )
        ).scalar_one()

    async def _dismiss():
        async with provider_env.admin_factory() as db:
            async with db.begin():
                await report_service.resolve_report(
                    db,
                    report_id=str(report_id),
                    action="dismiss",
                    admin_id=admin,
                    reason="r",
                    request_id=None,
                )

    await _dismiss()
    with pytest.raises(AgentCraftError) as excinfo:
        await _dismiss()
    assert excinfo.value.code == ErrorCode.REPORT_ALREADY_RESOLVED
    assert excinfo.value.http_status == 409


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_takedown_publishes_reversal(pg, provider_env):
    author = await seed_active_user(pg, "td-author@x.com")
    reporter = await seed_active_user(pg, "td-reporter@x.com")
    admin = await _seed_admin(pg)
    content = dict(EXPERT_CONTENT)
    _, revision_id = await seed_entity_with_revision(
        pg,
        author,
        "experts",
        entity_status="published",
        revision_status="published",
        content_json=content,
        content_sha256=content_sha256(content),
        with_pointer=True,
    )
    async with pg.engine.begin() as conn:  # 举报该公开 revision
        report_id = (
            await conn.execute(
                text(
                    "INSERT INTO reports (id, reporter_id, target_type, target_id, status, reason) "
                    "VALUES (gen_random_uuid(), CAST(:u AS uuid), 'expert_revision', "
                    "CAST(:r AS uuid), 'open', '违规') RETURNING id"
                ),  # status NOT NULL 且无 server_default（0001:361）
                {"u": reporter, "r": revision_id},
            )
        ).scalar_one()
    async with provider_env.admin_factory() as db:
        async with db.begin():
            out = await report_service.resolve_report(
                db,
                report_id=str(report_id),
                action="takedown_revision",
                admin_id=admin,
                reason="确认违规",
                request_id="req-4",
            )
    assert out["status"] == "actioned"
    async with pg.engine.begin() as conn:  # D15/D4 反向：指针 NULL + 实体回 draft
        row = (
            await conn.execute(
                text(
                    "SELECT status, published_revision_id FROM experts "
                    "WHERE published_revision_id IS NULL AND id = ("
                    "  SELECT expert_id FROM expert_revisions WHERE id = CAST(:r AS uuid))"
                ),
                {"r": revision_id},
            )
        ).one()
        assert row.status == "draft"
        assert (
            await conn.execute(
                text("SELECT status FROM expert_revisions WHERE id = CAST(:r AS uuid)"),
                {"r": revision_id},
            )
        ).scalar_one() == "archived"
        assert (
            await conn.execute(
                text("SELECT status FROM reports WHERE id = CAST(:p AS uuid)"), {"p": report_id}
            )
        ).scalar_one() == "actioned"
        assert (
            await conn.execute(
                text("SELECT count(*) FROM audit_logs WHERE action = 'report.takedown'")
            )
        ).scalar_one() == 1


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_takedown_rollback_leaves_no_trace(pg, provider_env):
    """原子性证据（review_service I1 强化同款范式）：takedown 在最终 flush 前已入队
    4 处写（published 归档 / 指针置空 / 实体回 draft / report actioned），AuditLog
    actor_id FK（ghost admin_id，users 无此行）在最终 flush 爆 → 整事务回滚。
    superuser 复核：指针/状态全无残留、audit_logs 0 行——区分「回滚了」与「从未写过」。"""
    author = await seed_active_user(pg, "td-rb-author@x.com")
    reporter = await seed_active_user(pg, "td-rb-reporter@x.com")
    content = dict(EXPERT_CONTENT)
    entity_id, revision_id = await seed_entity_with_revision(
        pg,
        author,
        "experts",
        entity_status="published",
        revision_status="published",
        content_json=content,
        content_sha256=content_sha256(content),
        with_pointer=True,
    )
    async with pg.engine.begin() as conn:  # superuser 造 open report（status 列必显式）
        report_id = (
            await conn.execute(
                text(
                    "INSERT INTO reports (id, reporter_id, target_type, target_id, status, reason) "
                    "VALUES (gen_random_uuid(), CAST(:u AS uuid), 'expert_revision', "
                    "CAST(:r AS uuid), 'open', '违规') RETURNING id"
                ),
                {"u": reporter, "r": revision_id},
            )
        ).scalar_one()
    ghost = str(uuid7())  # 格式合法但 users 无此行 → 最终 flush 时 AuditLog.actor_id FK 违例
    async with provider_env.admin_factory() as db:
        with pytest.raises(IntegrityError):
            async with db.begin():
                await report_service.resolve_report(
                    db,
                    report_id=str(report_id),
                    action="takedown_revision",
                    admin_id=ghost,
                    reason="确认违规",
                    request_id="req-rb",
                )
    async with pg.engine.begin() as conn:  # superuser 复核：4 处写 + 审计全数回滚
        row = (
            await conn.execute(
                text(
                    "SELECT status, published_revision_id FROM experts WHERE id = CAST(:e AS uuid)"
                ),
                {"e": entity_id},
            )
        ).one()
        assert row.status == "published"
        assert str(row.published_revision_id) == revision_id  # 指针未动
        assert (
            await conn.execute(
                text("SELECT status FROM expert_revisions WHERE id = CAST(:r AS uuid)"),
                {"r": revision_id},
            )
        ).scalar_one() == "published"
        assert (
            await conn.execute(
                text("SELECT status FROM reports WHERE id = CAST(:p AS uuid)"), {"p": report_id}
            )
        ).scalar_one() == "open"
        assert (await conn.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one() == 0


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_takedown_without_published_pointer_409(pg, provider_env):
    author = await seed_active_user(pg, "td-none@x.com")
    reporter = await seed_active_user(pg, "td-none2@x.com")
    admin = await _seed_admin(pg)
    _, revision_id = await seed_entity_with_revision(
        pg, author, "experts", entity_status="draft", revision_status="draft"
    )
    async with pg.engine.begin() as conn:
        report_id = (
            await conn.execute(
                text(
                    "INSERT INTO reports (id, reporter_id, target_type, target_id, status, reason) "
                    "VALUES (gen_random_uuid(), CAST(:u AS uuid), 'expert_revision', "
                    "CAST(:r AS uuid), 'open', '违规') RETURNING id"
                ),  # status NOT NULL 且无 server_default（0001:361）
                {"u": reporter, "r": revision_id},
            )
        ).scalar_one()
    async with provider_env.admin_factory() as db:
        async with db.begin():
            with pytest.raises(AgentCraftError) as excinfo:
                await report_service.resolve_report(
                    db,
                    report_id=str(report_id),
                    action="takedown_revision",
                    admin_id=admin,
                    reason="r",
                    request_id=None,
                )
    assert excinfo.value.code == ErrorCode.REVISION_NOT_PUBLISHED


# ---------- Phase 7 T5：ban_author（原子分支纯 admin_db，无 runtime/级联）----------


async def _seed_report(pg, reporter_id: str, target_type: str, target_id: str) -> str:
    """superuser 造 open report（status NOT NULL 且无 server_default，0001:361）。"""
    async with pg.engine.begin() as conn:
        return str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO reports (id, reporter_id, target_type, target_id, status, "
                        "reason) VALUES (gen_random_uuid(), CAST(:u AS uuid), :tt, "
                        "CAST(:t AS uuid), 'open', '测试') RETURNING id"
                    ),
                    {"u": reporter_id, "tt": target_type, "t": target_id},
                )
            ).scalar_one()
        )


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_resolve_ban_author_actioned_author_suspended_entitlement_revoked(pg, provider_env):
    """ban_author 原子分支全链行值：report actioned + 作者 suspended +
    expert_author 活跃行硬删（revoke_entitlement=True）+ 审计 report.ban_author
    （detail 含 report_id/target_user_id）。纯 admin_db——无级联段。"""
    author = str(await seed_active_user(pg, "ban-author@x.com"))
    reporter = str(await seed_active_user(pg, "ban-reporter@x.com"))
    admin = await _seed_admin(pg)
    await seed_entitlement(pg, author)
    _, revision_id = await seed_entity_with_revision(
        pg, author, "experts", entity_status="published", revision_status="published"
    )
    report_id = await _seed_report(pg, reporter, "expert_revision", revision_id)
    async with provider_env.admin_factory() as db:
        async with db.begin():
            out = await report_service.resolve_report(
                db,
                report_id=report_id,
                action="ban_author",
                admin_id=admin,
                reason="屡次违规",
                request_id="req-ban",
            )
    assert out == {
        "report_id": report_id,
        "status": "actioned",
        "banned_user_id": author,
        "suspension": {
            "user_id": author,
            "before_status": "active",
            "status": "suspended",
            "deadline_cleared": False,
            "entitlement_revoked": True,
        },
    }
    async with pg.engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT status FROM users WHERE id = CAST(:u AS uuid)"), {"u": author}
            )
        ).scalar_one() == "suspended"
        assert (
            await conn.execute(
                text(
                    "SELECT count(*) FROM user_entitlements "
                    "WHERE user_id = CAST(:u AS uuid) AND revoked_at IS NULL"
                ),
                {"u": author},
            )
        ).scalar_one() == 0
        assert (
            await conn.execute(
                text("SELECT status FROM reports WHERE id = CAST(:p AS uuid)"), {"p": report_id}
            )
        ).scalar_one() == "actioned"
        audit = (
            (
                await conn.execute(
                    text(
                        "SELECT actor_id, target_id, reason, detail FROM audit_logs "
                        "WHERE action = 'report.ban_author'"
                    )
                )
            )
            .mappings()
            .one()
        )
    assert str(audit["actor_id"]) == str(admin)
    assert str(audit["target_id"]) == report_id
    assert audit["reason"] == "屡次违规"
    assert audit["detail"] == {"report_id": report_id, "target_user_id": author}


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_resolve_ban_author_without_entitlement_zero_row_tolerance(pg, provider_env):
    """无 entitlement 的作者也可被 ban（D3 0 行容忍）：成功且 suspension 无
    entitlement_revoked 键。"""
    author = str(await seed_active_user(pg, "ban-noent@x.com"))
    reporter = str(await seed_active_user(pg, "ban-noent2@x.com"))
    admin = await _seed_admin(pg)
    _, revision_id = await seed_entity_with_revision(
        pg, author, "skills", entity_status="published", revision_status="published"
    )
    report_id = await _seed_report(pg, reporter, "skill_revision", revision_id)
    async with provider_env.admin_factory() as db:
        async with db.begin():
            out = await report_service.resolve_report(
                db,
                report_id=report_id,
                action="ban_author",
                admin_id=admin,
                reason="r",
                request_id=None,
            )
    assert out["status"] == "actioned"
    assert out["banned_user_id"] == author
    assert out["suspension"] == {
        "user_id": author,
        "before_status": "active",
        "status": "suspended",
        "deadline_cleared": False,
    }


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_resolve_ban_author_message_target_400(pg, provider_env):
    """message 举报目标无作者可封（task_messages.author 为 AI 角色非 user）：
    400 VALIDATION_ERROR，report 保持 open，零审计零副作用。"""
    reporter = str(await seed_active_user(pg, "ban-msg@x.com"))
    admin = await _seed_admin(pg)
    provider_id = await seed_provider(pg, reporter)
    task_id = await seed_task_for_provider(pg, reporter, provider_id)
    async with pg.engine.begin() as conn:
        assistant_msg = (
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), CAST(:t AS uuid), "
                    "CAST(:u AS uuid), 99, 'assistant', 'AI 回复') RETURNING id"
                ),
                {"t": task_id, "u": reporter},
            )
        ).scalar_one()
    report_id = await _seed_report(pg, reporter, "message", str(assistant_msg))
    async with provider_env.admin_factory() as db:
        async with db.begin():
            with pytest.raises(HTTPException) as excinfo:
                await report_service.resolve_report(
                    db,
                    report_id=report_id,
                    action="ban_author",
                    admin_id=admin,
                    reason="r",
                    request_id=None,
                )
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "VALIDATION_ERROR"
    async with pg.engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT status FROM reports WHERE id = CAST(:p AS uuid)"), {"p": report_id}
            )
        ).scalar_one() == "open"
        assert (await conn.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one() == 0


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_resolve_ban_author_missing_revision_404(pg, provider_env):
    """举报目标 revision 不存在（报告行指向幽灵 id）→ 404，report 保持 open。"""
    reporter = str(await seed_active_user(pg, "ban-404@x.com"))
    admin = await _seed_admin(pg)
    report_id = await _seed_report(pg, reporter, "expert_revision", str(uuid7()))
    async with provider_env.admin_factory() as db:
        async with db.begin():
            with pytest.raises(HTTPException) as excinfo:
                await report_service.resolve_report(
                    db,
                    report_id=report_id,
                    action="ban_author",
                    admin_id=admin,
                    reason="r",
                    request_id=None,
                )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == "NOT_FOUND"
    async with pg.engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT status FROM reports WHERE id = CAST(:p AS uuid)"), {"p": report_id}
            )
        ).scalar_one() == "open"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_resolve_ban_author_suspended_author_409_no_side_effects(pg, provider_env):
    """作者已 suspended（状态非 active|deleting）→ 409 USER_STATUS_CONFLICT
    （suspend_user_atomic 分流失守透传 HTTP 面）：report 保持 open、零审计。"""
    author = str(await seed_active_user(pg, "ban-susp@x.com"))
    reporter = str(await seed_active_user(pg, "ban-susp2@x.com"))
    admin = await _seed_admin(pg)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET status = 'suspended' WHERE id = CAST(:u AS uuid)"),
            {"u": author},
        )
    _, revision_id = await seed_entity_with_revision(
        pg, author, "experts", entity_status="published", revision_status="published"
    )
    report_id = await _seed_report(pg, reporter, "expert_revision", revision_id)
    async with provider_env.admin_factory() as db:
        async with db.begin():
            with pytest.raises(HTTPException) as excinfo:
                await report_service.resolve_report(
                    db,
                    report_id=report_id,
                    action="ban_author",
                    admin_id=admin,
                    reason="r",
                    request_id=None,
                )
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["code"] == "USER_STATUS_CONFLICT"
    async with pg.engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT status FROM reports WHERE id = CAST(:p AS uuid)"), {"p": report_id}
            )
        ).scalar_one() == "open"
        assert (await conn.execute(text("SELECT count(*) FROM audit_logs"))).scalar_one() == 0
