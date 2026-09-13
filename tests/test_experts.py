"""专家管理 + 专家中心 API 测试（Engineering Spec §6.3/§6.4 + PRD §4.2 + DB 设计 §5.2）。

- 字段规则按 PRD §4.2.2：name 2-30（去空格）、description 10-100（去空格）、
  category 枚举、persona/methodology 非空白（不强制上限）、task_examples ≤5 条每条 ≤50 字符
- 发布条件（§4.2.4 + §6.3）：至少绑定一个 published 且 enabled 的 Skill
- 绑定规则（§6.3）：Skill 必须 published；绑定默认 enabled=false；enabled=true
  需当前内容通过 validate_skill；重复绑定 409；绑定不等于启用
- 删除前置：无任何状态任务引用（快照规则：有任务时不可删）
- 专家中心（§6.4）：仅 published 可见，匿名可访问；详情只暴露 enabled Skill
"""

import asyncio
import json

import pytest

from backend.models.task import Task
from tests.test_skills import auth_header, create_skill, register_expert

pytestmark = pytest.mark.usefixtures("client")


def valid_expert(**overrides):
    payload = {
        "name": "技术周报专家",
        "description": "负责整理团队技术周报的编辑专家",
        "avatar_url": None,
        "category": "tech",
        "persona": "一名严谨的资深技术编辑，擅长从零散素材中提炼主线。",
        "methodology": "先收集素材，再按主题归类，最后输出结构化摘要与风险提示。",
        "task_examples": ["整理本周技术周报", "汇总多个工单的共性问题"],
    }
    payload.update(overrides)
    return payload


def create_expert(client, token, **overrides):
    return client.post("/api/experts", json=valid_expert(**overrides), headers=auth_header(token))


def make_published_skill(client, token, **overrides):
    response = create_skill(client, token, **overrides)
    assert response.status_code == 201
    skill_id = response.json()["data"]["id"]
    published = client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    assert published.status_code == 200
    return skill_id


def bind_skill(client, token, expert_id, skill_id, **body):
    payload = {"skill_id": skill_id, **body}
    return client.post(f"/api/experts/{expert_id}/skills", json=payload, headers=auth_header(token))


def seed_task(test_db, user_id, expert_id):
    async def _seed():
        async with test_db.session_factory() as session:
            session.add(
                Task(
                    user_id=user_id,
                    expert_id=expert_id,
                    expert_name_snapshot="技术周报专家",
                    title="整理本周技术周报",
                    status="created",
                    skill_snapshot=json.dumps({"skills": [], "loaded_at": "2026-09-02T00:00:00Z"}),
                    mcp_snapshot=json.dumps({"tools": [], "loaded_at": "2026-09-02T00:00:00Z"}),
                    workdir="/workspaces/authorized",
                )
            )
            await session.commit()

    asyncio.run(_seed())


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------


def test_experts_require_authentication(client):
    assert client.post("/api/experts", json=valid_expert()).status_code == 401
    assert client.get("/api/experts").status_code == 401


def test_experts_require_expert_role(client):
    registered = client.post(
        "/api/auth/register",
        json={"username": "plainex", "email": "plainex@example.com", "password": "secret123"},
    ).json()["data"]
    headers = auth_header(registered["token"])
    assert client.post("/api/experts", json=valid_expert(), headers=headers).status_code == 403
    assert client.get("/api/experts", headers=headers).status_code == 403


# ---------------------------------------------------------------------------
# 创建与字段规则（PRD §4.2.2）
# ---------------------------------------------------------------------------


def test_create_expert_returns_draft(client):
    token, _ = register_expert(client)
    response = create_expert(client, token)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["id"] > 0
    assert data["status"] == "draft"
    assert data["name"] == "技术周报专家"
    assert data["task_examples"] == ["整理本周技术周报", "汇总多个工单的共性问题"]


def test_create_expert_strips_name_and_description(client):
    token, _ = register_expert(client)
    response = create_expert(
        client, token, name="  周报专家  ", description="  整理团队周报的编辑专家  "
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["name"] == "周报专家"
    assert data["description"] == "整理团队周报的编辑专家"


def test_create_rejects_name_out_of_range(client):
    token, _ = register_expert(client)
    assert create_expert(client, token, name="名").status_code == 400
    assert create_expert(client, token, name="x" * 31).status_code == 400


def test_create_rejects_description_out_of_range(client):
    token, _ = register_expert(client)
    assert create_expert(client, token, description="九个字以内的简介").status_code == 400
    assert create_expert(client, token, description="长" * 101).status_code == 400


def test_create_rejects_invalid_category(client):
    token, _ = register_expert(client)
    assert create_expert(client, token, category="magic").status_code == 400


def test_create_rejects_blank_persona(client):
    token, _ = register_expert(client)
    assert create_expert(client, token, persona="   \n ").status_code == 400


def test_create_rejects_task_examples_over_limits(client):
    token, _ = register_expert(client)
    six = ["示例" * 5] * 6
    assert create_expert(client, token, task_examples=six).status_code == 400
    overlong = ["这" * 51]
    assert create_expert(client, token, task_examples=overlong).status_code == 400


def test_create_rejects_non_http_avatar(client):
    token, _ = register_expert(client)
    assert create_expert(client, token, avatar_url="ftp://example.com/a.png").status_code == 400
    ok = create_expert(client, token, avatar_url="https://example.com/a.png")
    assert ok.status_code == 201


# ---------------------------------------------------------------------------
# 列表 / 详情 / 更新
# ---------------------------------------------------------------------------


def test_list_returns_own_experts_with_status_filter(client):
    token, _ = register_expert(client)
    create_expert(client, token, name="草稿专家")
    published_id = create_expert(client, token, name="已发布专家").json()["data"]["id"]

    empty = client.get("/api/experts", headers=auth_header(token))
    assert empty.status_code == 200
    assert empty.json()["total"] == 2

    # 发布需要满足前置：绑定并启用一个已发布 Skill
    skill_id = make_published_skill(client, token)
    bind_skill(client, token, published_id, skill_id, enabled=True)
    assert (
        client.post(f"/api/experts/{published_id}/publish", headers=auth_header(token)).status_code
        == 200
    )
    listed = client.get("/api/experts", headers=auth_header(token)).json()
    assert listed["total"] == 2

    filtered = client.get(
        "/api/experts?status=published,offline", headers=auth_header(token)
    ).json()
    assert filtered["total"] == 1
    assert filtered["data"][0]["name"] == "已发布专家"

    offline = client.get("/api/experts?status=draft", headers=auth_header(token)).json()
    assert [item["name"] for item in offline["data"]] == ["草稿专家"]


def test_detail_includes_bound_skills_without_connection_info(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    bind_skill(client, token, expert_id, skill_id, enabled=True)

    response = client.get(f"/api/experts/{expert_id}", headers=auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["skills"][0]["id"] == skill_id
    assert data["skills"][0]["enabled"] is True
    # 用户 MCP 面已下线（Phase 5 T5）；连接信息（command/env_vars）不回显的隐私口径保留
    # （avatar_url 键名合法含 url）
    assert "command" not in json.dumps(data)
    assert "env_vars" not in json.dumps(data)


def test_detail_other_users_expert_forbidden(client):
    owner, _ = register_expert(client)
    stranger, _ = register_expert(client, username="exstranger", email="exstranger@example.com")
    expert_id = create_expert(client, owner).json()["data"]["id"]
    response = client.get(f"/api/experts/{expert_id}", headers=auth_header(stranger))
    assert response.status_code == 403


def test_detail_missing_expert_not_found(client):
    token, _ = register_expert(client)
    assert client.get("/api/experts/999", headers=auth_header(token)).status_code == 404


def test_update_expert_partial_fields(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    response = client.put(
        f"/api/experts/{expert_id}",
        json={"persona": "更加注重数据准确性的技术编辑，对数字敏感。"},
        headers=auth_header(token),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["persona"].startswith("更加注重")
    assert data["name"] == "技术周报专家"

    # 显式传 null 清空任务示例；缺席则保持不变
    cleared = client.put(
        f"/api/experts/{expert_id}",
        json={"task_examples": None},
        headers=auth_header(token),
    )
    assert cleared.status_code == 200
    assert cleared.json()["data"]["task_examples"] is None

    untouched = client.put(
        f"/api/experts/{expert_id}",
        json={"name": "改名的周报专家"},
        headers=auth_header(token),
    )
    assert untouched.json()["data"]["task_examples"] is None


def test_update_rejects_explicit_null_on_required_fields(client):
    # 不可清除字段（含 category）显式 null 应 400，而非落库触发 NOT NULL 的 500
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    for field in ("name", "description", "category", "persona", "methodology"):
        response = client.put(
            f"/api/experts/{expert_id}", json={field: None}, headers=auth_header(token)
        )
        assert response.status_code == 400, field


def test_discover_skill_count_matches_detail(client):
    # 卡片计数与详情列表使用同一公开口径（enabled + published）：
    # 绑定启用的 Skill 下架后，两者应一致地归零
    owner, _ = register_expert(client)
    expert_id = create_expert(client, owner).json()["data"]["id"]
    skill_id = make_published_skill(client, owner)
    bind_skill(client, owner, expert_id, skill_id, enabled=True)
    client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(owner))

    before = client.get("/api/discover/experts").json()["data"][0]
    assert before["skill_count"] == 1

    assert (
        client.post(f"/api/skills/{skill_id}/offline", headers=auth_header(owner)).status_code
        == 200
    )
    card = client.get("/api/discover/experts").json()["data"][0]
    detail = client.get(f"/api/discover/experts/{expert_id}").json()["data"]
    assert card["skill_count"] == 0
    assert detail["skills"] == []


def test_update_other_users_expert_forbidden(client):
    owner, _ = register_expert(client)
    stranger, _ = register_expert(client, username="exstranger2", email="exstranger2@example.com")
    expert_id = create_expert(client, owner).json()["data"]["id"]
    response = client.put(
        f"/api/experts/{expert_id}", json={"name": "篡改"}, headers=auth_header(stranger)
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 发布 / 下架 / 删除（§4.2.4-§4.2.6）
# ---------------------------------------------------------------------------


def test_publish_requires_enabled_published_skill(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]

    no_binding = client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(token))
    assert no_binding.status_code == 400
    assert no_binding.json()["error"]["code"] == "EXPERT_PUBLISH_CONDITION"

    skill_id = make_published_skill(client, token)
    bind_skill(client, token, expert_id, skill_id)  # 默认 enabled=false
    disabled = client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(token))
    assert disabled.status_code == 400


def test_publish_with_enabled_skill_then_conflict(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    bind_skill(client, token, expert_id, skill_id, enabled=True)

    published = client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(token))
    assert published.status_code == 200
    assert published.json()["data"]["status"] == "published"

    again = client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(token))
    assert again.status_code == 409


def test_publish_blocked_when_bound_skill_offline(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    bind_skill(client, token, expert_id, skill_id, enabled=True)
    assert (
        client.post(f"/api/skills/{skill_id}/offline", headers=auth_header(token)).status_code
        == 200
    )
    response = client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(token))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "EXPERT_PUBLISH_CONDITION"


def test_offline_published_only(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    assert (
        client.post(f"/api/experts/{expert_id}/offline", headers=auth_header(token)).status_code
        == 409
    )

    skill_id = make_published_skill(client, token)
    bind_skill(client, token, expert_id, skill_id, enabled=True)
    client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(token))
    offline = client.post(f"/api/experts/{expert_id}/offline", headers=auth_header(token))
    assert offline.status_code == 200
    assert offline.json()["data"]["status"] == "offline"
    assert (
        client.post(f"/api/experts/{expert_id}/offline", headers=auth_header(token)).status_code
        == 409
    )


def test_delete_without_task_references(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    bind_skill(client, token, expert_id, skill_id)

    response = client.delete(f"/api/experts/{expert_id}", headers=auth_header(token))
    assert response.status_code == 200
    assert response.json()["data"]["message"] == "deleted"
    assert client.get(f"/api/experts/{expert_id}", headers=auth_header(token)).status_code == 404
    # Skill 本身不受影响，可再次绑定到其他专家
    assert client.get(f"/api/skills/{skill_id}", headers=auth_header(token)).status_code == 200


def test_delete_blocked_by_task_reference(client, test_db):
    token, user_id = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    seed_task(test_db, user_id, expert_id)

    response = client.delete(f"/api/experts/{expert_id}", headers=auth_header(token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXPERT_STILL_REFERENCED"


def test_delete_other_users_expert_forbidden(client):
    owner, _ = register_expert(client)
    stranger, _ = register_expert(client, username="exstranger3", email="exstranger3@example.com")
    expert_id = create_expert(client, owner).json()["data"]["id"]
    response = client.delete(f"/api/experts/{expert_id}", headers=auth_header(stranger))
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Skill 绑定 / 启用 / 解绑（§6.3）
# ---------------------------------------------------------------------------


def test_bind_defaults_to_disabled(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    response = bind_skill(client, token, expert_id, skill_id)
    assert response.status_code == 201
    assert response.json()["data"] == {
        "expert_id": expert_id,
        "skill_id": skill_id,
        "enabled": False,
    }


def test_bind_draft_skill_rejected(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = create_skill(client, token).json()["data"]["id"]  # draft
    response = bind_skill(client, token, expert_id, skill_id)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SKILL_NOT_PUBLISHED"


def test_bind_offline_skill_rejected(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    client.post(f"/api/skills/{skill_id}/offline", headers=auth_header(token))
    response = bind_skill(client, token, expert_id, skill_id)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SKILL_NOT_PUBLISHED"


def test_bind_others_skill_forbidden(client):
    owner, _ = register_expert(client)
    stranger, _ = register_expert(client, username="skillowner", email="skillowner@example.com")
    expert_id = create_expert(client, owner).json()["data"]["id"]
    foreign_skill = make_published_skill(client, stranger)
    response = bind_skill(client, owner, expert_id, foreign_skill)
    assert response.status_code == 403


def test_bind_missing_skill_not_found(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    assert bind_skill(client, token, expert_id, 999).status_code == 404


def test_bind_duplicate_conflicts(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    assert bind_skill(client, token, expert_id, skill_id).status_code == 201
    assert bind_skill(client, token, expert_id, skill_id).status_code == 409


def test_bind_with_enabled_true_succeeds(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    # published 内容恒通过 validate_skill（发布与已发布编辑均已强制），
    # enabled=true 时服务端仍会执行内容校验（规格 §6.3 防御性要求）
    skill_id = make_published_skill(client, token)
    response = bind_skill(client, token, expert_id, skill_id, enabled=True)
    assert response.status_code == 201
    assert response.json()["data"]["enabled"] is True


def test_update_binding_toggle(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    bind_skill(client, token, expert_id, skill_id)

    enabled = client.put(
        f"/api/experts/{expert_id}/skills/{skill_id}",
        json={"enabled": True},
        headers=auth_header(token),
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["enabled"] is True

    disabled = client.put(
        f"/api/experts/{expert_id}/skills/{skill_id}",
        json={"enabled": False},
        headers=auth_header(token),
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["enabled"] is False


def test_update_binding_missing_conflict(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    response = client.put(
        f"/api/experts/{expert_id}/skills/{skill_id}",
        json={"enabled": True},
        headers=auth_header(token),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BINDING_NOT_FOUND"

    # 不存在的 skill_id 同样返回绑定不存在（错误优先级：绑定 → 归属）
    missing = client.put(
        f"/api/experts/{expert_id}/skills/999",
        json={"enabled": True},
        headers=auth_header(token),
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "BINDING_NOT_FOUND"


def test_unbind_then_rebind(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    bind_skill(client, token, expert_id, skill_id)

    unbound = client.delete(
        f"/api/experts/{expert_id}/skills/{skill_id}", headers=auth_header(token)
    )
    assert unbound.status_code == 200
    assert unbound.json()["data"]["message"] == "unbound"

    detail = client.get(f"/api/experts/{expert_id}", headers=auth_header(token)).json()["data"]
    assert detail["skills"] == []

    # 解绑后可重新绑定
    assert bind_skill(client, token, expert_id, skill_id).status_code == 201


def test_unbind_missing_conflicts(client):
    token, _ = register_expert(client)
    expert_id = create_expert(client, token).json()["data"]["id"]
    skill_id = make_published_skill(client, token)
    response = client.delete(
        f"/api/experts/{expert_id}/skills/{skill_id}", headers=auth_header(token)
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 专家中心（§6.4，匿名可访问）
# ---------------------------------------------------------------------------


def test_discover_lists_published_only(client):
    owner, _ = register_expert(client)
    create_expert(client, owner, name="隐藏草稿专家")
    published_id = create_expert(client, owner, name="公开周报专家").json()["data"]["id"]
    offline_id = create_expert(client, owner, name="隐藏下架专家").json()["data"]["id"]
    skill_id = make_published_skill(client, owner)
    bind_skill(client, owner, published_id, skill_id, enabled=True)
    client.post(f"/api/experts/{published_id}/publish", headers=auth_header(owner))
    bind_skill(client, owner, offline_id, skill_id, enabled=True)
    client.post(f"/api/experts/{offline_id}/publish", headers=auth_header(owner))
    client.post(f"/api/experts/{offline_id}/offline", headers=auth_header(owner))

    response = client.get("/api/discover/experts")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    card = body["data"][0]
    assert card["name"] == "公开周报专家"
    assert card["skill_count"] == 1
    assert {"id", "name", "description", "avatar_url", "category", "skill_count"} <= set(card)


def test_discover_search_and_category(client):
    owner, _ = register_expert(client)
    target_id = create_expert(client, owner, name="周报整理专家").json()["data"]["id"]
    create_expert(client, owner, name="数据看板专家", category="data_analysis")
    create_expert(client, owner, name="别的专家", description="专门写产品文案简介的帮手")
    for expert_id in (target_id,):
        skill_id = make_published_skill(client, owner)
        bind_skill(client, owner, expert_id, skill_id, enabled=True)
        client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(owner))

    by_name = client.get("/api/discover/experts?search=周报").json()
    assert [item["name"] for item in by_name["data"]] == ["周报整理专家"]

    by_category = client.get("/api/discover/experts?category=data_analysis")
    assert by_category.status_code == 200
    assert by_category.json()["total"] == 0  # 未发布的不可见


def test_discover_detail_exposes_enabled_skills_and_examples(client):
    owner, _ = register_expert(client)
    expert_id = create_expert(client, owner).json()["data"]["id"]
    make_published_skill(client, owner)  # 已发布但未绑定，不应出现在详情中
    bound_skill = make_published_skill(client, owner, name="绑定启用的技能")
    bind_skill(client, owner, expert_id, bound_skill, enabled=True)
    client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(owner))

    response = client.get(f"/api/discover/experts/{expert_id}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [skill["id"] for skill in data["skills"]] == [bound_skill]
    assert data["task_examples"] == ["整理本周技术周报", "汇总多个工单的共性问题"]
    # P04 详情页需要展示人设与方法论
    assert data["persona"].startswith("一名严谨")
    assert data["methodology"].startswith("先收集素材")


def test_discover_detail_hides_non_published(client):
    owner, _ = register_expert(client)
    draft_id = create_expert(client, owner).json()["data"]["id"]
    assert client.get(f"/api/discover/experts/{draft_id}").status_code == 404
