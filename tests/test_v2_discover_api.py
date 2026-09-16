"""discover 匿名面测试（D19：游客仅见 published；搜索/分页/限流）。"""

import json
import uuid as _uuid

import pytest
from sqlalchemy import text

from backend.v2.content_hash import content_sha256
from tests.v2_content_helpers import (
    EXPERT_CONTENT,
    SKILL_CONTENT,
    seed_entity_with_revision,
    seed_revision_tools,
)
from tests.v2_provider_helpers import auth_client, seed_active_user


async def _published_expert(pg, email: str, name: str, category: str = "tech"):
    author = await seed_active_user(pg, email)
    content = dict(EXPERT_CONTENT, name=name, category=category)
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
    return author, revision_id


async def _entity_id_by_revision(pg, revision_id: str) -> str:
    async with pg.engine.begin() as conn:
        return str(
            (
                await conn.execute(
                    text("SELECT id FROM experts WHERE published_revision_id = CAST(:r AS uuid)"),
                    {"r": revision_id},
                )
            ).scalar_one()
        )


async def _backfill_expert_content(pg, revision_id: str, content: dict) -> None:
    """superuser 回填 expert revision 的 content_json（重算 hash；冻结触发器仅拦 app 上下文）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE expert_revisions SET content_json = CAST(:c AS jsonb), "
                "content_sha256 = :h WHERE id = CAST(:r AS uuid)"
            ),
            {
                "c": json.dumps(content, ensure_ascii=False),
                "h": content_sha256(content),
                "r": revision_id,
            },
        )


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_guest_sees_only_published(pg, provider_env):
    await _published_expert(pg, "disc-pub@x.com", "公开专家甲")
    author = await seed_active_user(pg, "disc-draft@x.com")
    await seed_entity_with_revision(pg, author, "experts")  # draft 不可见
    client = auth_client()
    resp = await client.get("/api/discover/experts")
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert [i["name"] for i in items] == ["公开专家甲"]
    assert items[0]["skill_count"] == 0  # D5：len(skill_refs)


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_search_and_category_filter(pg, provider_env):
    await _published_expert(pg, "disc-s1@x.com", "数据库调优专家")
    await _published_expert(pg, "disc-s2@x.com", "文案写作专家", category="writing")
    client = auth_client()
    hit = await client.get("/api/discover/experts", params={"search": "数据库"})
    assert [i["name"] for i in hit.json()["data"]["items"]] == ["数据库调优专家"]
    cat = await client.get("/api/discover/experts", params={"category": "writing"})
    assert [i["name"] for i in cat.json()["data"]["items"]] == ["文案写作专家"]
    none = await client.get("/api/discover/experts", params={"search": "不存在的词组"})
    assert none.json()["data"]["items"] == []


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_detail_includes_skills_tools_and_notice(pg, provider_env):
    author, revision_id = await _published_expert(pg, "disc-detail@x.com", "工具型专家")
    content = dict(EXPERT_CONTENT, name="工具型专家")
    content["skill_refs"] = []  # 本例先验 tools；skills 解析用例见镜像表
    await seed_revision_tools(pg, revision_id, [("check_code_style", "1")])
    client = auth_client()
    entity_id = await _entity_id_by_revision(pg, revision_id)
    resp = await client.get(f"/api/discover/experts/{entity_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["tools"] == [{"tool_id": "check_code_style", "version": "1"}]
    assert "AI" in data["ai_generated_notice"] or "ai" in data["ai_generated_notice"]


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_draft_detail_404(pg, provider_env):
    author = await seed_active_user(pg, "disc-404@x.com")
    entity_id, _ = await seed_entity_with_revision(pg, author, "experts")  # draft
    client = auth_client()
    resp = await client.get(f"/api/discover/experts/{entity_id}")
    assert resp.status_code == 404


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_discover_rate_limited_60_per_hour(pg, provider_env):
    client = auth_client()
    for i in range(60):
        resp = await client.get("/api/discover/experts")
        assert resp.status_code == 200, f"第 {i} 次即被限流"
    resp = await client.get("/api/discover/experts")
    assert resp.status_code == 429
    # 详情端点同桶限流（D12：列表与详情都挂 enforce）——合法 UUID 也先被限流拦下
    detail = await client.get(f"/api/discover/experts/{_uuid.uuid4()}")
    assert detail.status_code == 429


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_search_param_capped(pg, provider_env):
    client = auth_client()
    resp = await client.get("/api/discover/experts", params={"search": "字" * 101})
    assert resp.status_code == 400  # Query(max_length=100) → RequestValidationError
    # → main.py 统一 handler 渲染 400 VALIDATION_ERROR 信封


# ---------- 镜像用例表 ----------


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_detail_resolves_skills(pg, provider_env):
    """content.skill_refs 引用他人 published skill revision → 详情列出三字段。"""
    skill_owner = await seed_active_user(pg, "disc-skill-owner@x.com")
    skill_content = dict(SKILL_CONTENT)
    skill_entity_id, skill_revision_id = await seed_entity_with_revision(
        pg,
        skill_owner,
        "skills",
        entity_status="published",
        revision_status="published",
        content_json=skill_content,
        content_sha256=content_sha256(skill_content),
        with_pointer=True,
    )
    _, revision_id = await _published_expert(pg, "disc-skill@x.com", "技能引用专家")
    content = dict(EXPERT_CONTENT, name="技能引用专家")
    content["skill_refs"] = [{"skill_id": skill_entity_id, "revision_id": skill_revision_id}]
    await _backfill_expert_content(pg, revision_id, content)
    entity_id = await _entity_id_by_revision(pg, revision_id)
    client = auth_client()
    resp = await client.get(f"/api/discover/experts/{entity_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["skills"] == [
        {"skill_id": skill_entity_id, "name": "代码评审技能", "revision_no": 1}
    ]


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_detail_unpublished_skill_ref_omitted(pg, provider_env):
    """skill_refs 引用 draft skill revision（发布后 skill 被 takedown 的退化形态）→ 静默剔除。"""
    skill_owner = await seed_active_user(pg, "disc-skill-draft-owner@x.com")
    skill_entity_id, skill_revision_id = await seed_entity_with_revision(
        pg, skill_owner, "skills"
    )  # draft：无 published 指针 → 匿名不可见
    _, revision_id = await _published_expert(pg, "disc-skill-draft@x.com", "引用下架技能专家")
    content = dict(EXPERT_CONTENT, name="引用下架技能专家")
    content["skill_refs"] = [{"skill_id": skill_entity_id, "revision_id": skill_revision_id}]
    await _backfill_expert_content(pg, revision_id, content)
    entity_id = await _entity_id_by_revision(pg, revision_id)
    client = auth_client()
    resp = await client.get(f"/api/discover/experts/{entity_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["skills"] == []
    assert data["name"] == "引用下架技能专家"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_pagination(pg, provider_env):
    for i in range(3):
        await _published_expert(pg, f"disc-page-{i}@x.com", f"分页专家{i}")
    client = auth_client()
    resp = await client.get("/api/discover/experts", params={"page": 2, "page_size": 2})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["items"]) == 1
    assert data["total"] == 3
    assert (data["page"], data["page_size"]) == (2, 2)


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_page_size_capped(pg, provider_env):
    client = auth_client()
    resp = await client.get("/api/discover/experts", params={"page_size": 51})
    assert resp.status_code == 400  # Query le=50 → 统一 handler 渲染 VALIDATION_ERROR 信封


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_list_and_detail_expose_published_revision_id(pg, provider_env):
    """Sup §10.2（Phase 8 T2）：列表项与详情均含 published_revision_id（前端召唤
    专家直用作 POST /tasks 的 expert_revision_id，免二次解析）；draft 实体不入
    列表（既有行为回归）。discover WHERE 实体 status='published' → 恒为 UUID 非空。"""
    author = await seed_active_user(pg, "disc-rev-id@x.com")
    content = dict(EXPERT_CONTENT, name="指针暴露专家")
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
    draft_author = await seed_active_user(pg, "disc-rev-id-draft@x.com")
    await seed_entity_with_revision(pg, draft_author, "experts")  # draft 不可见
    client = auth_client()
    listing = await client.get("/api/discover/experts")
    assert listing.status_code == 200
    items = listing.json()["data"]["items"]
    assert [i["name"] for i in items] == ["指针暴露专家"]
    assert items[0]["published_revision_id"] == revision_id
    entity_id = await _entity_id_by_revision(pg, revision_id)
    detail = await client.get(f"/api/discover/experts/{entity_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["published_revision_id"] == revision_id
