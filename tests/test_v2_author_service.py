"""author_service 测试（owner 引擎 + entitlement 门 + draft 两段式）。

形态：每个用例独立 async def，直接吃 pg + provider_env 夹具（provider_env 由
conftest re-export 全局可见，注入双 role runtime + 依赖 override）。
"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.v2 import author_service, idempotency
from tests.v2_content_helpers import (
    EXPERT_CONTENT,
    SKILL_CONTENT,
    seed_entitlement,
    seed_entity_with_revision,
)
from tests.v2_provider_helpers import seed_active_user

# ---------- 核心用例（brief Step 2 全文）----------


@pytest.mark.asyncio
async def test_create_expert_without_entitlement_forbidden(pg, provider_env):
    user_id = await seed_active_user(pg, "no-ent@x.com")
    with pytest.raises(HTTPException) as excinfo:
        await author_service.create_entity(
            provider_env,
            user_id=user_id,
            target="experts",
            content=dict(EXPERT_CONTENT),
            idem_key="k1",
            idem_hash=idempotency.request_hash(EXPERT_CONTENT),
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_create_expert_creates_entity_and_draft_revision(pg, provider_env):
    user_id = await seed_active_user(pg, "author@x.com")
    await seed_entitlement(pg, user_id)
    detail = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="k1",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    assert detail["entity"]["status"] == "draft"
    assert detail["revision"]["revision_no"] == 1
    assert detail["revision"]["status"] == "draft"
    assert len(detail["revision"]["content_sha256"]) == 64
    # hash 与 content 一致（T2 算法）
    from backend.v2.content_hash import content_sha256

    assert detail["revision"]["content_sha256"] == content_sha256(
        detail["revision"]["content_json"]
    )


@pytest.mark.asyncio
async def test_create_expert_idempotent_replay(pg, provider_env):
    user_id = await seed_active_user(pg, "replay@x.com")
    await seed_entitlement(pg, user_id)
    body = dict(EXPERT_CONTENT)
    first = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=body,
        idem_key="k1",
        idem_hash=idempotency.request_hash(body),
    )
    replay = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=body,
        idem_key="k1",
        idem_hash=idempotency.request_hash(body),
    )
    assert isinstance(replay, author_service.Replay)
    assert (
        replay.response_json["data"]["revision"]["revision_id"] == first["revision"]["revision_id"]
    )


@pytest.mark.asyncio
async def test_edit_overwrites_draft_then_creates_new(pg, provider_env):
    from backend.v2.content_hash import content_sha256

    user_id = await seed_active_user(pg, "editor@x.com")
    await seed_entitlement(pg, user_id)
    first = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="k1",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    entity_id = first["entity"]["id"]
    # 最新 revision 是 draft → 覆写（revision_id/revision_no 不变，内容与 hash 更新）
    edited = dict(EXPERT_CONTENT, persona="改后的 persona。")
    out = await author_service.edit_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        entity_id=entity_id,
        content=edited,
        idem_key="k2",
        idem_hash=idempotency.request_hash(edited),
    )
    assert out["revision"]["revision_id"] == first["revision"]["revision_id"]
    assert out["revision"]["revision_no"] == 1
    assert out["revision"]["content_sha256"] == content_sha256(edited)
    # 提审后（T4 的 submit 在此用 superuser 直改 status 模拟）→ 编辑新建 no=2 draft
    await _superuser_set_revision_status(pg, first["revision"]["revision_id"], "pending_review")
    out2 = await author_service.edit_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        entity_id=entity_id,
        content=edited,
        idem_key="k3",
        idem_hash=idempotency.request_hash(edited),
    )
    assert out2["revision"]["revision_no"] == 2
    assert out2["revision"]["status"] == "draft"


@pytest.mark.asyncio
async def test_get_entity_cross_owner_404(pg, provider_env):
    from backend.v2.runtime import owner_session

    owner = await seed_active_user(pg, "owner@x.com")
    stranger = await seed_active_user(pg, "stranger@x.com")
    await seed_entitlement(pg, owner)
    detail = await author_service.create_entity(
        provider_env,
        user_id=owner,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="k1",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    # get_entity 吃调用方 owner_session 会话（Interfaces 约定）；统一 404 走
    # HTTPException 双轨（D 系 Global Constraints + T3 注记 1），非 AgentCraftError
    async with owner_session(provider_env, stranger) as db:
        with pytest.raises(HTTPException) as excinfo:
            await author_service.get_entity(
                db, user_id=stranger, target="experts", entity_id=detail["entity"]["id"]
            )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_create_skill_with_bad_skill_ref_rejected(pg, provider_env):
    user_id = await seed_active_user(pg, "refbad@x.com")
    await seed_entitlement(pg, user_id)
    content = dict(
        EXPERT_CONTENT,
        skill_refs=[{"skill_id": str(uuid.uuid4()), "revision_id": str(uuid.uuid4())}],
    )
    with pytest.raises(HTTPException) as excinfo:
        await author_service.create_entity(
            provider_env,
            user_id=user_id,
            target="experts",
            content=content,
            idem_key="k1",
            idem_hash=idempotency.request_hash(content),
        )
    assert excinfo.value.status_code == 400


async def _superuser_set_revision_status(pg, revision_id: str, status: str) -> None:
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE expert_revisions SET status = :s WHERE id = CAST(:r AS uuid)"),
            {"s": status, "r": revision_id},
        )


# ---------- 镜像用例（brief Step 6 用例表；skills 域 + 边界）----------


@pytest.mark.asyncio
async def test_create_skill_happy(pg, provider_env):
    user_id = await seed_active_user(pg, "skill-author@x.com")
    await seed_entitlement(pg, user_id)
    detail = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="skills",
        content=dict(SKILL_CONTENT),
        idem_key="k1",
        idem_hash=idempotency.request_hash(SKILL_CONTENT),
    )
    assert detail["entity"]["status"] == "draft"
    assert detail["revision"]["revision_no"] == 1
    assert detail["revision"]["status"] == "draft"
    from backend.v2.content_hash import content_sha256

    assert detail["revision"]["content_sha256"] == content_sha256(SKILL_CONTENT)


@pytest.mark.asyncio
async def test_create_content_too_large_400(pg, provider_env):
    user_id = await seed_active_user(pg, "toolarge@x.com")
    await seed_entitlement(pg, user_id)
    content = dict(EXPERT_CONTENT, persona="字" * 30000)  # 3 字节/字 ≈ 90KB > 64KiB
    with pytest.raises(HTTPException) as excinfo:
        await author_service.create_entity(
            provider_env,
            user_id=user_id,
            target="experts",
            content=content,
            idem_key="k1",
            idem_hash=idempotency.request_hash(content),
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_create_skill_ref_to_published_foreign_ok(pg, provider_env):
    user_id = await seed_active_user(pg, "refok@x.com")
    foreign = await seed_active_user(pg, "refok-foreign@x.com")
    await seed_entitlement(pg, user_id)
    skill_id, revision_id = await seed_entity_with_revision(
        pg,
        foreign,
        "skills",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
        content_json={"name": "公开技能"},
    )
    content = dict(EXPERT_CONTENT, skill_refs=[{"skill_id": skill_id, "revision_id": revision_id}])
    detail = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=content,
        idem_key="k1",
        idem_hash=idempotency.request_hash(content),
    )
    assert detail["entity"]["status"] == "draft"
    assert detail["revision"]["revision_no"] == 1


@pytest.mark.asyncio
async def test_create_skill_ref_to_foreign_draft_400(pg, provider_env):
    user_id = await seed_active_user(pg, "refforeign@x.com")
    foreign = await seed_active_user(pg, "refforeign-f@x.com")
    await seed_entitlement(pg, user_id)
    skill_id, revision_id = await seed_entity_with_revision(pg, foreign, "skills")  # 他人 draft
    content = dict(EXPERT_CONTENT, skill_refs=[{"skill_id": skill_id, "revision_id": revision_id}])
    with pytest.raises(HTTPException) as excinfo:
        await author_service.create_entity(
            provider_env,
            user_id=user_id,
            target="experts",
            content=content,
            idem_key="k1",
            idem_hash=idempotency.request_hash(content),
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_create_skill_ref_own_draft_ok(pg, provider_env):
    user_id = await seed_active_user(pg, "refown@x.com")
    await seed_entitlement(pg, user_id)
    skill_id, revision_id = await seed_entity_with_revision(pg, user_id, "skills")  # 本人 draft
    content = dict(EXPERT_CONTENT, skill_refs=[{"skill_id": skill_id, "revision_id": revision_id}])
    detail = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=content,
        idem_key="k1",
        idem_hash=idempotency.request_hash(content),
    )
    assert detail["revision"]["revision_no"] == 1


@pytest.mark.asyncio
async def test_list_entities_status_filter(pg, provider_env):
    from backend.v2.runtime import owner_session

    user_id = await seed_active_user(pg, "lister@x.com")
    await seed_entitlement(pg, user_id)
    detail = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="k1",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    await seed_entity_with_revision(
        pg,
        user_id,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
        content_json={"name": "已发布专家"},
    )
    async with owner_session(provider_env, user_id) as db:
        items = await author_service.list_entities(
            db, user_id=user_id, target="experts", status=None
        )
        drafts = await author_service.list_entities(
            db, user_id=user_id, target="experts", status="draft"
        )
    assert len(items) == 2  # status=None 不过滤（own RLS）
    draft_item = next(i for i in items if i["status"] == "draft")
    published_item = next(i for i in items if i["status"] == "published")
    assert draft_item["id"] == detail["entity"]["id"]
    assert draft_item["name"] == "架构评审专家"
    assert draft_item["revision_count"] == 1
    assert draft_item["published_revision_id"] is None
    assert draft_item["latest_revision"]["revision_no"] == 1
    assert published_item["name"] == "已发布专家"
    assert (
        published_item["published_revision_id"] == published_item["latest_revision"]["revision_id"]
    )
    assert {i["id"] for i in drafts} == {draft_item["id"]}


@pytest.mark.asyncio
async def test_list_entities_invalid_status_400(pg, provider_env):
    """status 白名单外 → 400（brief 注记 3；T7 端点镜像用例亦钉此形）。"""
    from backend.v2.runtime import owner_session

    user_id = await seed_active_user(pg, "badstatus@x.com")
    async with owner_session(provider_env, user_id) as db:
        with pytest.raises(HTTPException) as excinfo:
            await author_service.list_entities(
                db, user_id=user_id, target="experts", status="pending_review"
            )
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_get_entity_detail_contains_revisions(pg, provider_env):
    from backend.v2.runtime import owner_session

    user_id = await seed_active_user(pg, "detailer@x.com")
    await seed_entitlement(pg, user_id)
    detail = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="k1",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    async with owner_session(provider_env, user_id) as db:
        got = await author_service.get_entity(
            db, user_id=user_id, target="experts", entity_id=detail["entity"]["id"]
        )
    # detail 键为域名词（Interfaces：{"expert": {...}, "revisions": [...]}）
    assert got["expert"]["id"] == detail["entity"]["id"]
    assert got["expert"]["status"] == "draft"
    assert len(got["revisions"]) == 1
    assert got["revisions"][0]["revision_no"] == 1
    assert got["revisions"][0]["content_json"]["name"] == "架构评审专家"


@pytest.mark.asyncio
async def test_edit_idempotent_replay(pg, provider_env):
    user_id = await seed_active_user(pg, "edit-replay@x.com")
    await seed_entitlement(pg, user_id)
    first = await author_service.create_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="k1",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    edited = dict(EXPERT_CONTENT, persona="改后的 persona。")
    out1 = await author_service.edit_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        entity_id=first["entity"]["id"],
        content=edited,
        idem_key="k2",
        idem_hash=idempotency.request_hash(edited),
    )
    out2 = await author_service.edit_entity(
        provider_env,
        user_id=user_id,
        target="experts",
        entity_id=first["entity"]["id"],
        content=edited,
        idem_key="k2",
        idem_hash=idempotency.request_hash(edited),
    )
    assert isinstance(out2, author_service.Replay)
    assert out2.response_json["data"]["revision"]["revision_id"] == out1["revision"]["revision_id"]


@pytest.mark.asyncio
async def test_invalid_entity_id_400(pg, provider_env):
    user_id = await seed_active_user(pg, "badid@x.com")
    await seed_entitlement(pg, user_id)
    with pytest.raises(HTTPException) as excinfo:
        await author_service.edit_entity(
            provider_env,
            user_id=user_id,
            target="experts",
            entity_id="not-a-uuid",
            content=dict(EXPERT_CONTENT),
            idem_key="k1",
            idem_hash=idempotency.request_hash(EXPERT_CONTENT),
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_edit_without_entitlement_403(pg, provider_env):
    user_id = await seed_active_user(pg, "noent-edit@x.com")
    with pytest.raises(HTTPException) as excinfo:
        await author_service.edit_entity(
            provider_env,
            user_id=user_id,
            target="experts",
            entity_id=str(uuid.uuid4()),
            content=dict(EXPERT_CONTENT),
            idem_key="k1",
            idem_hash=idempotency.request_hash(EXPERT_CONTENT),
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_list_entities_excludes_foreign_published(pg, provider_env):
    """D6「本人列表」：他人 published 实体虽在 RLS 发布可见面（先以裸查询取证），
    list_entities 的服务层 owner 过滤必须剔除——双向互不可见。"""
    from backend.v2.runtime import owner_session

    author_a = await seed_active_user(pg, "list-a@x.com")
    author_b = await seed_active_user(pg, "list-b@x.com")
    await seed_entitlement(pg, author_a)
    await seed_entitlement(pg, author_b)
    detail_a = await author_service.create_entity(
        provider_env,
        user_id=author_a,
        target="experts",
        content=dict(EXPERT_CONTENT),
        idem_key="ka",
        idem_hash=idempotency.request_hash(EXPERT_CONTENT),
    )
    await seed_entity_with_revision(
        pg,
        author_b,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
        content_json={"name": "B 的公开专家"},
    )
    async with owner_session(provider_env, author_a) as db:
        # RLS 取证：发布可见面确实含他人 published 实体（2 行 = 本人 draft + 他人 published）
        n_visible = (
            await db.execute(text("SELECT count(*) FROM experts WHERE status = 'published'"))
        ).scalar_one()
        assert n_visible == 1  # B 的 published 行对 A 可见 → 剔除只能靠服务层
        items = await author_service.list_entities(
            db, user_id=author_a, target="experts", status=None
        )
    assert [i["id"] for i in items] == [detail_a["entity"]["id"]]
    # 反向：B 的列表不含 A 的实体
    async with owner_session(provider_env, author_b) as db:
        items_b = await author_service.list_entities(
            db, user_id=author_b, target="experts", status=None
        )
    assert [i["name"] for i in items_b] == ["B 的公开专家"]


@pytest.mark.asyncio
async def test_get_entity_foreign_published_404(pg, provider_env):
    """D6「本人详情」：他人 published 实体在 RLS 发布可见面，get_entity 须
    owner 过滤收窄 → 统一 404（与其他不可见形态一致）。"""
    from backend.v2.runtime import owner_session

    author = await seed_active_user(pg, "get-a@x.com")
    foreign = await seed_active_user(pg, "get-b@x.com")
    foreign_eid, _ = await seed_entity_with_revision(
        pg,
        foreign,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
        content_json={"name": "B 的公开专家"},
    )
    async with owner_session(provider_env, author) as db:
        # RLS 取证：该行对 A 可见 → 404 只能来自服务层 owner 过滤
        n = (
            await db.execute(
                text("SELECT count(*) FROM experts WHERE id = CAST(:i AS uuid)"),
                {"i": foreign_eid},
            )
        ).scalar_one()
        assert n == 1
        with pytest.raises(HTTPException) as excinfo:
            await author_service.get_entity(
                db, user_id=author, target="experts", entity_id=foreign_eid
            )
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail["code"] == "NOT_FOUND"
