"""作者面生命周期测试（Phase 8 T6，D3a 裁决）：offline/DELETE + 迁移 0011 护栏回归。

Part A（SQL 级护栏回归）：owner 上下文实体 published→draft 翻转与实体删除级联
内部写（published_revision_id SET NULL / revision_tools 级联 DELETE）在迁移 0011
前必 P0001（契约审查 C1 实证）→ 0011 后放行；其余冻结语义（指针/状态词表/
revision 内容/工具集直接改写）钉死不变。

Part B（服务级，brief ①-⑫ 全清单）：offline/DELETE 端点行为、ENTITY_IN_USE
引用统计、幂等重放红线（begin 先于实体查询）、四枚审计登记、harness 工具闸。
"""

import uuid as _uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.v2 import author_service, idempotency
from tests.conftest import APP_ROLE, make_role_engine
from tests.v2_content_helpers import (
    EXPERT_CONTENT,
    seed_entitlement,
    seed_entity_with_revision,
    seed_revision_tools,
)
from tests.v2_provider_helpers import (
    auth_client,
    login,
    seed_active_user,
    seed_provider,
)

_NO_BODY_HASH = idempotency.request_hash(None)


async def _author(pg, email: str) -> str:
    user_id = await seed_active_user(pg, email)
    await seed_entitlement(pg, user_id)
    return user_id


async def _superuser_scalar(pg, sql: str, params: dict | None = None):
    async with pg.engine.begin() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _superuser_row(pg, sql: str, params: dict | None = None):
    async with pg.engine.begin() as conn:
        return (await conn.execute(text(sql), params or {})).first()


# ========== Part A：迁移 0011 护栏回归（owner 上下文生命周期放行） ==========


async def test_owner_context_entity_offline_flip_allowed(pg):
    """owner 上下文实体 status published→draft（offline 原语）放行，指针保留。

    迁移 0011 前：0006 guard 对实体 status 变更无条件 RAISE（本用例红）；
    0011 后仅放行该单向翻转（draft→published 等照旧拦，见下）。"""
    owner = await seed_active_user(pg, "lc-flip@x.com")
    eid, rid = await seed_entity_with_revision(
        pg,
        owner,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    app = make_role_engine(pg, APP_ROLE)
    try:
        async with app.begin() as conn:  # begin：事务提交跨块持久化（connect 回滚）
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            updated = await conn.execute(
                text("UPDATE experts SET status = 'draft' WHERE id = CAST(:x AS uuid)"),
                {"x": eid},
            )
            assert updated.rowcount == 1
    finally:
        await app.dispose()
    row = await _superuser_row(
        pg,
        "SELECT status::text AS s, published_revision_id::text AS p "
        "FROM experts WHERE id = CAST(:x AS uuid)",
        {"x": eid},
    )
    assert row.s == "draft"
    assert row.p == rid  # offline 不清发布指针（Sup §10.6）


@pytest.mark.parametrize("frm,to", [("draft", "published"), ("published", "archived")])
async def test_owner_context_entity_status_still_frozen(pg, frm, to):
    """0011 词表冻结不变：published→draft 之外的状态变更照旧 P0001（自我发布
    面封死；archived 不复活）。每条失败语句独立连接块（单块单失败纪律）。"""
    owner = await seed_active_user(pg, f"lc-frozen-{frm}-{to}@x.com")
    eid, _ = await seed_entity_with_revision(pg, owner, "experts", entity_status=frm)
    app = make_role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="禁止修改发布指针或实体状态"):
                await conn.execute(
                    text("UPDATE experts SET status = :s WHERE id = CAST(:x AS uuid)"),
                    {"s": to, "x": eid},
                )
    finally:
        await app.dispose()


async def test_owner_context_entity_pointer_still_frozen(pg):
    """0011 直接改写（depth=1）发布指针照旧 P0001——pg_trigger_depth()>1 放行
    通道只覆盖 FK/级联引发的内部 UPDATE，不开放 owner 直接改写。"""
    owner = await seed_active_user(pg, "lc-ptr@x.com")
    eid, _ = await seed_entity_with_revision(
        pg,
        owner,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    app = make_role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="禁止修改发布指针或实体状态"):
                await conn.execute(
                    text(
                        "UPDATE experts SET published_revision_id = NULL "
                        "WHERE id = CAST(:x AS uuid)"
                    ),
                    {"x": eid},
                )
    finally:
        await app.dispose()


async def test_owner_context_revision_delete_cascade_internal_writes_allowed(pg):
    """owner 上下文删 revision：published_revision_id SET NULL 内部 UPDATE（0006）
    与 revision_tools 级联 DELETE（0007）由 pg_trigger_depth()>1 放行（迁移 0011
    前两者皆 P0001，DELETE 整体失败）。实体行与指针置空结果钉死。"""
    owner = await seed_active_user(pg, "lc-revdel@x.com")
    eid, rid = await seed_entity_with_revision(
        pg,
        owner,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    await seed_revision_tools(pg, rid, [("check_code_style", "1")])
    app = make_role_engine(pg, APP_ROLE)
    try:
        async with app.begin() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            deleted = await conn.execute(
                text("DELETE FROM expert_revisions WHERE id = CAST(:r AS uuid)"),
                {"r": rid},
            )
            assert deleted.rowcount == 1
    finally:
        await app.dispose()
    n_tools = await _superuser_scalar(
        pg,
        "SELECT count(*) FROM revision_tools WHERE expert_revision_id = CAST(:r AS uuid)",
        {"r": rid},
    )
    assert n_tools == 0  # 级联删净
    row = await _superuser_row(
        pg,
        "SELECT published_revision_id::text AS p FROM experts WHERE id = CAST(:x AS uuid)",
        {"x": eid},
    )
    assert row.p is None  # FK SET NULL 生效


async def test_owner_context_entity_delete_cascade_allowed(pg):
    """owner 上下文物理删实体：expert_revisions 级联删（FK CASCADE）→
    revision_tools 再级联（depth=3，0007 guard 放行）。迁移 0011 前级联触发器
    P0001 令 DELETE 整体失败。"""
    owner = await seed_active_user(pg, "lc-entdel@x.com")
    eid, rid = await seed_entity_with_revision(
        pg,
        owner,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    await seed_revision_tools(pg, rid, [("check_code_style", "1")])
    app = make_role_engine(pg, APP_ROLE)
    try:
        async with app.begin() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            deleted = await conn.execute(
                text("DELETE FROM experts WHERE id = CAST(:x AS uuid)"), {"x": eid}
            )
            assert deleted.rowcount == 1
    finally:
        await app.dispose()
    counts = await _superuser_row(
        pg,
        "SELECT (SELECT count(*) FROM experts WHERE id = CAST(:x AS uuid)) AS e, "
        "(SELECT count(*) FROM expert_revisions WHERE expert_id = CAST(:x AS uuid)) AS r, "
        "(SELECT count(*) FROM revision_tools WHERE expert_revision_id = CAST(:rr AS uuid)) AS t",
        {"x": eid, "rr": rid},
    )
    assert tuple(counts) == (0, 0, 0)


async def test_owner_context_direct_tools_delete_still_frozen(pg):
    """0007 主语义不变：depth=1 的 owner 直接删非 draft 父行工具行照旧 RAISE。"""
    owner = await seed_active_user(pg, "lc-tooldel@x.com")
    _, rid = await seed_entity_with_revision(pg, owner, "experts", revision_status="pending_review")
    await seed_revision_tools(pg, rid, [("check_code_style", "1")])
    app = make_role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="父 revision 非 draft"):
                await conn.execute(
                    text("DELETE FROM revision_tools WHERE expert_revision_id = CAST(:r AS uuid)"),
                    {"r": rid},
                )
    finally:
        await app.dispose()


async def test_owner_context_revision_content_freeze_intact(pg):
    """0006 主语义不变：非 draft revision 内容冻结照旧（0011 只放行实体生命周期
    两分支，revision 冻结词表零改动）。"""
    owner = await seed_active_user(pg, "lc-freeze@x.com")
    _, rid = await seed_entity_with_revision(pg, owner, "experts", revision_status="published")
    app = make_role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="revision 提交后内容不可变"):
                await conn.execute(
                    # \: 转义：text() 会把裸 :1 当 bindparam（0002 迁移注记同款坑）
                    text(
                        "UPDATE expert_revisions SET content_json = '{\"x\"\:1}' "
                        "WHERE id = CAST(:r AS uuid)"
                    ),
                    {"r": rid},
                )
    finally:
        await app.dispose()


# ========== Part B：offline/DELETE 服务用例（brief ①-⑫） ==========


@pytest.mark.asyncio
async def test_offline_published_expert_to_draft(pg, provider_env):
    """①offline：published → 200 {data:{entity}}（status=draft、指针保留）、
    discover 立即不可见、审计 expert.offline、幂等重放同响应。"""
    from backend.v2.runtime import owner_session

    author = await _author(pg, "lc-off-pub@x.com")
    eid, rid = await seed_entity_with_revision(
        pg,
        author,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
        content_json={"name": "公开专家"},
    )
    out = await author_service.offline_entity(
        provider_env,
        user_id=author,
        target="experts",
        entity_id=eid,
        idem_key="off-1",
        idem_hash=_NO_BODY_HASH,
    )
    assert out["entity"]["status"] == "draft"
    assert out["entity"]["published_revision_id"] == rid  # 指针保留
    # discover 立即不可见（游客视角回归断言）
    client = auth_client()
    resp = await client.get("/api/discover/experts")
    assert resp.status_code == 200
    assert resp.json()["data"]["items"] == []
    # 同事务审计（四枚登记之一）
    row = await _superuser_row(
        pg,
        "SELECT actor_id::text AS a, target_id::text AS tid, detail FROM audit_logs "
        "WHERE action = 'expert.offline'",
    )
    assert row is not None
    assert row.a == str(author)  # ::text 出参
    assert row.tid == eid  # 别名避 Row.t（tuple 访问器）冲突
    assert row.detail["entity_id"] == eid
    assert row.detail["kind"] == "expert"
    # 幂等重放同响应
    replay = await author_service.offline_entity(
        provider_env,
        user_id=author,
        target="experts",
        entity_id=eid,
        idem_key="off-1",
        idem_hash=_NO_BODY_HASH,
    )
    assert isinstance(replay, author_service.Replay)
    assert replay.response_json["data"]["entity"]["id"] == eid
    # offline 后作者列表仍可见（本人 draft）
    async with owner_session(provider_env, author) as db:
        items = await author_service.list_entities(
            db, user_id=author, target="experts", status=None
        )
    assert [i["status"] for i in items] == ["draft"]


@pytest.mark.asyncio
async def test_offline_draft_skill_idempotent_success(pg, provider_env):
    """②draft 实体 offline = 幂等成功（已处目标态）；skill.offline 审计登记；
    换 key 再调仍成功（幂等成功语义独立于幂等记录）。"""
    author = await _author(pg, "lc-off-draft@x.com")
    sid, _ = await seed_entity_with_revision(pg, author, "skills")  # draft/draft
    out = await author_service.offline_entity(
        provider_env,
        user_id=author,
        target="skills",
        entity_id=sid,
        idem_key="off-s1",
        idem_hash=_NO_BODY_HASH,
    )
    assert out["entity"]["status"] == "draft"
    out2 = await author_service.offline_entity(
        provider_env,
        user_id=author,
        target="skills",
        entity_id=sid,
        idem_key="off-s2",
        idem_hash=_NO_BODY_HASH,
    )
    assert out2["entity"]["status"] == "draft"
    row = await _superuser_row(pg, "SELECT detail FROM audit_logs WHERE action = 'skill.offline'")
    assert row is not None
    assert row.detail["entity_id"] == sid
    assert row.detail["kind"] == "skill"


@pytest.mark.asyncio
async def test_delete_unreferenced_expert(pg, provider_env):
    """③DELETE 无引用实体 → 200 且实体+revisions 物理消失；expert.delete 审计
    detail 携引用计数 0。"""
    author = await _author(pg, "lc-del@x.com")
    detail = await author_service.create_entity(
        provider_env,
        user_id=author,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="d0",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    eid = detail["entity"]["id"]
    out = await author_service.delete_entity(
        provider_env,
        user_id=author,
        target="experts",
        entity_id=eid,
        idem_key="d1",
        idem_hash=_NO_BODY_HASH,
    )
    assert out["deleted"] is True
    assert out["entity"]["id"] == eid
    counts = await _superuser_row(
        pg,
        "SELECT (SELECT count(*) FROM experts WHERE id = CAST(:x AS uuid)) AS e, "
        "(SELECT count(*) FROM expert_revisions WHERE expert_id = CAST(:x AS uuid)) AS r",
        {"x": eid},
    )
    assert (counts.e, counts.r) == (0, 0)
    row = await _superuser_row(pg, "SELECT detail FROM audit_logs WHERE action = 'expert.delete'")
    assert row is not None
    assert row.detail["entity_id"] == eid
    assert row.detail["kind"] == "expert"
    assert row.detail["reference_count"] == 0


async def _seed_task_on_revision(pg, task_owner: str, revision_id: str) -> str:
    """superuser 造引用指定 revision 的任务行（RESTRICT 引用面）。"""
    provider_id = await seed_provider(pg, task_owner)
    async with pg.engine.begin() as conn:
        catalog_id = (
            await conn.execute(
                text("SELECT catalog_id FROM user_providers WHERE id = CAST(:p AS uuid)"),
                {"p": provider_id},
            )
        ).scalar_one()
        return str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO tasks (id, owner_id, expert_revision_id, provider_id, "
                        "provider_catalog_id, provider_model_id, provider_key_version, "
                        "event_sequence, status) VALUES (gen_random_uuid(), CAST(:u AS uuid), "
                        "CAST(:r AS uuid), CAST(:p AS uuid), CAST(:c AS uuid), 'm', 1, 0, "
                        "'queued') RETURNING id"
                    ),
                    {"u": task_owner, "r": revision_id, "p": provider_id, "c": catalog_id},
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_delete_task_referenced_expert_409(pg, provider_env):
    """④DELETE 被任务引用的 expert → 409 ENTITY_IN_USE，文案仅引用计数
    （「被 N 个任务引用」，零他人标识）；实体行保留。"""
    author = await _author(pg, "lc-del-task@x.com")
    eid, rid = await seed_entity_with_revision(
        pg,
        author,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    task_owner = await seed_active_user(pg, "lc-task-owner@x.com")
    await _seed_task_on_revision(pg, task_owner, rid)
    with pytest.raises(AgentCraftError) as excinfo:
        await author_service.delete_entity(
            provider_env,
            user_id=author,
            target="experts",
            entity_id=eid,
            idem_key="d2",
            idem_hash=_NO_BODY_HASH,
        )
    assert excinfo.value.code == ErrorCode.ENTITY_IN_USE
    assert excinfo.value.http_status == 409
    assert excinfo.value.message == "被 1 个任务引用"
    n = await _superuser_scalar(
        pg, "SELECT count(*) FROM experts WHERE id = CAST(:x AS uuid)", {"x": eid}
    )
    assert n == 1  # 实体保留


@pytest.mark.asyncio
async def test_delete_skill_referenced_by_foreign_draft_409(pg, provider_env):
    """⑤DELETE 被他人 expert skill_refs（draft，owner 上下文不可见——计数必须
    权威读）引用的 skill → 409 ENTITY_IN_USE「被 N 个专家引用」。"""
    author = await _author(pg, "lc-del-skill@x.com")
    sid, srid = await seed_entity_with_revision(
        pg,
        author,
        "skills",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
        content_json={"name": "被引用技能"},
    )
    ref_author = await seed_active_user(pg, "lc-ref-author@x.com")
    await seed_entity_with_revision(
        pg,
        ref_author,
        "experts",
        content_json={"name": "引用方", "skill_refs": [{"skill_id": sid, "revision_id": srid}]},
    )
    with pytest.raises(AgentCraftError) as excinfo:
        await author_service.delete_entity(
            provider_env,
            user_id=author,
            target="skills",
            entity_id=sid,
            idem_key="d3",
            idem_hash=_NO_BODY_HASH,
        )
    assert excinfo.value.code == ErrorCode.ENTITY_IN_USE
    assert excinfo.value.http_status == 409
    assert excinfo.value.message == "被 1 个专家引用"
    n = await _superuser_scalar(
        pg, "SELECT count(*) FROM skills WHERE id = CAST(:x AS uuid)", {"x": sid}
    )
    assert n == 1


@pytest.mark.asyncio
async def test_delete_unreferenced_skill_ok(pg, provider_env):
    """⑤补：无引用 skill DELETE 成功（skills 域物理删 + skill.delete 审计）。"""
    author = await _author(pg, "lc-del-skill-ok@x.com")
    sid, _ = await seed_entity_with_revision(pg, author, "skills")
    out = await author_service.delete_entity(
        provider_env,
        user_id=author,
        target="skills",
        entity_id=sid,
        idem_key="d4",
        idem_hash=_NO_BODY_HASH,
    )
    assert out["deleted"] is True
    n = await _superuser_scalar(
        pg, "SELECT count(*) FROM skills WHERE id = CAST(:x AS uuid)", {"x": sid}
    )
    assert n == 0
    row = await _superuser_row(pg, "SELECT detail FROM audit_logs WHERE action = 'skill.delete'")
    assert row is not None
    assert row.detail["reference_count"] == 0


@pytest.mark.asyncio
async def test_offline_and_delete_cross_owner_404(pg, provider_env):
    """⑥非 owner（有 entitlement，过 403 门）offline/DELETE 他人实体 → 404 统一。"""
    victim = await seed_active_user(pg, "lc-victim@x.com")
    stranger = await _author(pg, "lc-stranger@x.com")
    eid, _ = await seed_entity_with_revision(
        pg,
        victim,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    with pytest.raises(HTTPException) as excinfo:
        await author_service.offline_entity(
            provider_env,
            user_id=stranger,
            target="experts",
            entity_id=eid,
            idem_key="x1",
            idem_hash=_NO_BODY_HASH,
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == "NOT_FOUND"
    with pytest.raises(HTTPException) as excinfo:
        await author_service.delete_entity(
            provider_env,
            user_id=stranger,
            target="experts",
            entity_id=eid,
            idem_key="x2",
            idem_hash=_NO_BODY_HASH,
        )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_offline_and_delete_without_entitlement_403(pg, provider_env):
    """⑦非 expert_author → 403 FORBIDDEN（家族一致；随机 id 证明门先于 404）。"""
    user_id = await seed_active_user(pg, "lc-noent@x.com")
    for func in (author_service.offline_entity, author_service.delete_entity):
        with pytest.raises(HTTPException) as excinfo:
            await func(
                provider_env,
                user_id=user_id,
                target="experts",
                entity_id=str(_uuid.uuid4()),
                idem_key="x3",
                idem_hash=_NO_BODY_HASH,
            )
        assert excinfo.value.status_code == 403
        assert excinfo.value.detail["code"] == "FORBIDDEN"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_offline_delete_require_idempotency_key(pg):
    """⑧幂等键必带（缺失 400 沿家族断言，HTTP 层依赖门先于 handler）。"""
    email = "lc-nokey@x.com"
    await seed_active_user(pg, email)
    await seed_entitlement(
        pg, await _superuser_scalar(pg, "SELECT id::text FROM users WHERE email = :e", {"e": email})
    )
    client = auth_client()
    await login(client, email, "User-Passw0rd!")
    missing = await client.post(f"/api/experts/{_uuid.uuid4()}/offline")
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"
    missing_del = await client.delete(f"/api/experts/{_uuid.uuid4()}")
    assert missing_del.status_code == 400
    assert missing_del.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_delete_replay_after_physical_delete(pg, provider_env):
    """⑨物理删后同 key 同 body 重放 → 200 原响应（幂等 begin 先于实体查询——
    §7「重放先于一切状态门」红线；行已不在，重放仍 200）。"""
    author = await _author(pg, "lc-del-rp@x.com")
    detail = await author_service.create_entity(
        provider_env,
        user_id=author,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="r0",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    eid = detail["entity"]["id"]
    first = await author_service.delete_entity(
        provider_env,
        user_id=author,
        target="experts",
        entity_id=eid,
        idem_key="r1",
        idem_hash=_NO_BODY_HASH,
    )
    n = await _superuser_scalar(
        pg, "SELECT count(*) FROM experts WHERE id = CAST(:x AS uuid)", {"x": eid}
    )
    assert n == 0  # 物理消失后重放
    replay = await author_service.delete_entity(
        provider_env,
        user_id=author,
        target="experts",
        entity_id=eid,
        idem_key="r1",
        idem_hash=_NO_BODY_HASH,
    )
    assert isinstance(replay, author_service.Replay)
    assert replay.status_code == 200
    assert replay.response_json["data"] == first


@pytest.mark.asyncio
async def test_submit_harness_tool_rejected(pg, provider_env):
    """⑫提审 tools 含 harness-kind（check_code_style@1 种子）→ 400
    VALIDATION_ERROR（平台工具不进作者工具集）；container-kind 工具照常通过
    （闸按 kind 划界，非一刀切禁平台目录）。"""
    user_id = await _author(pg, "lc-harness@x.com")
    detail = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="h0",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    with pytest.raises(HTTPException) as excinfo:
        await author_service.submit_revision(
            provider_env,
            user_id=user_id,
            target="experts",
            entity_id=detail["entity"]["id"],
            revision_id=detail["revision"]["revision_id"],
            tools=[{"tool_id": "check_code_style", "version": "1"}],
            idem_key="h1",
            idem_hash=idempotency.request_hash(
                {"tools": [{"tool_id": "check_code_style", "version": "1"}]}
            ),
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "VALIDATION_ERROR"
    out = await author_service.submit_revision(
        provider_env,
        user_id=user_id,
        target="experts",
        entity_id=detail["entity"]["id"],
        revision_id=detail["revision"]["revision_id"],
        tools=[{"tool_id": "read_task_file", "version": "1"}],
        idem_key="h2",
        idem_hash=idempotency.request_hash(
            {"tools": [{"tool_id": "read_task_file", "version": "1"}]}
        ),
    )
    assert out["revision"]["status"] == "pending_review"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_offline_endpoint_happy_path(pg):
    """HTTP 面：POST /experts/{id}/offline → 200 {data:{entity}}，重放无
    Set-Cookie（家族形态）。"""
    email = "lc-http-off@x.com"
    user_id = await seed_active_user(pg, email)
    await seed_entitlement(pg, user_id)
    client = auth_client()
    await login(client, email, "User-Passw0rd!")
    created = (
        await client.post(
            "/api/experts",
            json=dict(EXPERT_CONTENT, name="下架对象"),
            headers={"Idempotency-Key": "ho0"},
        )
    ).json()["data"]
    # 造 published：HTTP 链提审后无审核者——直接 superuser 置 published（冻结
    # 触发器仅拦 owner 上下文，superuser 放行，同既有测试形态）
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE experts SET status='published', published_revision_id = "
                "CAST(:r AS uuid) WHERE id = CAST(:e AS uuid)"
            ),
            {"r": created["revision"]["revision_id"], "e": created["entity"]["id"]},
        )
        await conn.execute(
            text("UPDATE expert_revisions SET status='published' WHERE id = CAST(:r AS uuid)"),
            {"r": created["revision"]["revision_id"]},
        )
    resp = await client.post(
        f"/api/experts/{created['entity']['id']}/offline",
        headers={"Idempotency-Key": "ho1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["entity"]["status"] == "draft"
    replay = await client.post(
        f"/api/experts/{created['entity']['id']}/offline",
        headers={"Idempotency-Key": "ho1"},
    )
    assert replay.status_code == 200
    assert replay.json() == resp.json()
    assert "set-cookie" not in {k.lower() for k in replay.headers.keys()}


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_delete_endpoint_roundtrip(pg):
    """HTTP 面：DELETE /experts/{id}（幂等键）→ 200，实体消失；同 key 重放
    200 原响应（HTTP 层 ⑨ 形态）。"""
    email = "lc-http-del@x.com"
    user_id = await seed_active_user(pg, email)
    await seed_entitlement(pg, user_id)
    client = auth_client()
    await login(client, email, "User-Passw0rd!")
    created = (
        await client.post(
            "/api/experts",
            json=dict(EXPERT_CONTENT, name="删除对象"),
            headers={"Idempotency-Key": "hd0"},
        )
    ).json()["data"]
    resp = await client.delete(
        f"/api/experts/{created['entity']['id']}",
        headers={"Idempotency-Key": "hd1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deleted"] is True
    replay = await client.delete(
        f"/api/experts/{created['entity']['id']}",
        headers={"Idempotency-Key": "hd1"},
    )
    assert replay.status_code == 200
    assert replay.json() == resp.json()
    n = await _superuser_scalar(
        pg,
        "SELECT count(*) FROM experts WHERE id = CAST(:x AS uuid)",
        {"x": created["entity"]["id"]},
    )
    assert n == 0
