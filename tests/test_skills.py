"""Skill 管理 API 测试（Engineering Spec §6.5 + PRD §4.4 + DB 设计 §5.3 状态机）。

- 全部端点要求专家身份：未登录 401，普通用户 403
- 字段规则按 PRD §4.4.2：name 2-30（去首尾空格）、description 10-200、
  role 5-200、长文本 20-5000 且不能全空白、input_requirements 可选 ≤5000
- 状态机：draft → published → offline → published；非法流转 409
- published 编辑必须通过 validate_skill，失败 400 并保留原内容
- 删除前置：已从所有专家解绑，否则 409
"""

import pytest

from backend.models.expert import Expert
from backend.models.expert_skill import ExpertSkill

pytestmark = pytest.mark.usefixtures("client")


def register_expert(client, username="skiller", email="skiller@example.com"):
    """注册并申请专家身份，返回 (token, user_id)。"""
    registered = client.post(
        "/api/auth/register",
        json={"username": username, "email": email, "password": "secret123"},
    ).json()["data"]
    token = registered["token"]
    client.post("/api/users/me/expert", headers=auth_header(token))
    return token, registered["id"]


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def valid_payload(**overrides):
    payload = {
        "name": "技术周报生成",
        "description": "生成结构化技术周报的能力包",
        "use_case": "团队需要在每周五前汇总本周技术进展并同步给相关成员。",
        "role": "一名资深技术编辑",
        "goal": "收集本周技术素材，归纳要点并输出结构化周报。",
        "steps": "1. 收集素材\n2. 按主题归类\n3. 撰写摘要。",
        "input_requirements": "本周的工单记录与会议纪要。",
        "output_requirements": "输出包含标题、要点、风险三段的周报正文。",
        "constraints": "不编造未提供的事实，语气保持中性、克制。",
    }
    payload.update(overrides)
    return payload


def create_skill(client, token, **overrides):
    return client.post("/api/skills", json=valid_payload(**overrides), headers=auth_header(token))


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------


def test_skills_require_authentication(client):
    assert client.post("/api/skills", json=valid_payload()).status_code == 401
    assert client.get("/api/skills").status_code == 401


def test_skills_require_expert_role(client):
    registered = client.post(
        "/api/auth/register",
        json={"username": "plain", "email": "plain@example.com", "password": "secret123"},
    ).json()["data"]
    headers = auth_header(registered["token"])
    assert client.post("/api/skills", json=valid_payload(), headers=headers).status_code == 403
    assert client.get("/api/skills", headers=headers).status_code == 403


# ---------------------------------------------------------------------------
# 创建（PRD §4.4.2 字段规则）
# ---------------------------------------------------------------------------


def test_create_skill_returns_draft(client):
    token, _ = register_expert(client)
    response = create_skill(client, token)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["id"] > 0
    assert data["status"] == "draft"
    assert data["name"] == "技术周报生成"
    assert data["created_at"]


def test_create_skill_strips_name_and_description(client):
    token, _ = register_expert(client)
    response = create_skill(
        client, token, name="  周报技能  ", description="  一句话描述这个技能的实际用途  "
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["name"] == "周报技能"
    assert data["description"] == "一句话描述这个技能的实际用途"


def test_create_rejects_name_out_of_range(client):
    token, _ = register_expert(client)
    assert create_skill(client, token, name="长").status_code == 400
    assert create_skill(client, token, name="x" * 31).status_code == 400


def test_create_rejects_description_out_of_range(client):
    token, _ = register_expert(client)
    assert create_skill(client, token, description="短").status_code == 400
    assert create_skill(client, token, description="长" * 201).status_code == 400


def test_create_rejects_role_out_of_range(client):
    token, _ = register_expert(client)
    assert create_skill(client, token, role="角色").status_code == 400
    assert create_skill(client, token, role="色" * 201).status_code == 400


def test_create_rejects_short_long_text_fields(client):
    token, _ = register_expert(client)
    for field in ("use_case", "goal", "steps", "output_requirements", "constraints"):
        assert create_skill(client, token, **{field: "太短了"}).status_code == 400, field


def test_create_rejects_whitespace_only_required_text(client):
    token, _ = register_expert(client)
    assert create_skill(client, token, goal="   \n  ").status_code == 400


def test_create_rejects_missing_required_field(client):
    token, _ = register_expert(client)
    payload = valid_payload()
    del payload["constraints"]
    assert client.post("/api/skills", json=payload, headers=auth_header(token)).status_code == 400


def test_create_rejects_overlong_text(client):
    token, _ = register_expert(client)
    assert create_skill(client, token, goal="目" * 5001).status_code == 400
    assert create_skill(client, token, input_requirements="入" * 5001).status_code == 400


# ---------------------------------------------------------------------------
# 列表 / 详情
# ---------------------------------------------------------------------------


def test_list_returns_pagination_envelope_and_only_own_skills(client):
    token, _ = register_expert(client)
    other, _ = register_expert(client, username="other", email="other@example.com")
    create_skill(client, token, name="我的技能一")
    create_skill(client, token, name="我的技能二")
    create_skill(client, other, name="别人的技能")

    response = client.get("/api/skills?page=1&size=10", headers=auth_header(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["size"] == 10
    # 列表按创建时间倒序（新创建的在前），与任务列表约定一致
    assert [item["name"] for item in body["data"]] == ["我的技能二", "我的技能一"]


def test_detail_includes_bound_experts(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    response = client.get(f"/api/skills/{skill_id}", headers=auth_header(token))
    assert response.status_code == 200
    assert response.json()["data"]["bound_experts"] == []


def test_detail_of_other_users_skill_forbidden(client):
    owner, _ = register_expert(client)
    stranger, _ = register_expert(client, username="stranger", email="stranger@example.com")
    skill_id = create_skill(client, owner).json()["data"]["id"]
    response = client.get(f"/api/skills/{skill_id}", headers=auth_header(stranger))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_detail_missing_skill_not_found(client):
    token, _ = register_expert(client)
    response = client.get("/api/skills/999", headers=auth_header(token))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# 更新
# ---------------------------------------------------------------------------


def test_update_draft_partial_fields(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    response = client.put(
        f"/api/skills/{skill_id}",
        json={"name": "改名后的技能", "input_requirements": None},
        headers=auth_header(token),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "改名后的技能"
    assert data["input_requirements"] is None
    assert data["status"] == "draft"
    assert data["goal"] == "收集本周技术素材，归纳要点并输出结构化周报。"


def test_update_published_with_invalid_content_returns_400_and_keeps_original(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    assert (
        client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token)).status_code
        == 200
    )

    failed = client.put(
        f"/api/skills/{skill_id}",
        json={"constraints": "包含密钥 sk-abcdefghijklmnopqrstuvwx"},
        headers=auth_header(token),
    )
    assert failed.status_code == 400
    assert failed.json()["error"]["code"] == "SKILL_INVALID"

    detail = client.get(f"/api/skills/{skill_id}", headers=auth_header(token)).json()["data"]
    assert detail["constraints"] == "不编造未提供的事实，语气保持中性、克制。"
    assert detail["status"] == "published"


def test_update_published_with_valid_content_succeeds(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    response = client.put(
        f"/api/skills/{skill_id}",
        json={"goal": "收集素材并在两小时内产出结构化周报初稿。"},
        headers=auth_header(token),
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "published"
    assert response.json()["data"]["goal"].startswith("收集素材并")


def test_update_rejects_explicit_null_on_required_fields(client):
    # 显式 JSON null 应回 400 拒绝，而非落库触发 NOT NULL 约束的 500
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    for field in ("name", "description", "use_case", "role", "goal", "constraints"):
        response = client.put(
            f"/api/skills/{skill_id}", json={field: None}, headers=auth_header(token)
        )
        assert response.status_code == 400, field
    # 可选字段允许显式置空
    clear_optional = client.put(
        f"/api/skills/{skill_id}", json={"input_requirements": None}, headers=auth_header(token)
    )
    assert clear_optional.status_code == 200


def test_update_other_users_skill_forbidden(client):
    owner, _ = register_expert(client)
    stranger, _ = register_expert(client, username="stranger2", email="stranger2@example.com")
    skill_id = create_skill(client, owner).json()["data"]["id"]
    response = client.put(
        f"/api/skills/{skill_id}", json={"name": "篡改"}, headers=auth_header(stranger)
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 发布 / 下架状态机（DB 设计 §5.3）
# ---------------------------------------------------------------------------


def test_publish_valid_draft(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    response = client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "published"


def test_publish_twice_conflicts(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    response = client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    assert response.status_code == 409


def test_publish_with_api_key_content_rejected(client):
    token, _ = register_expert(client)
    skill_id = create_skill(
        client, token, constraints="密钥可以直接写进约束里：sk-abcdefghijklmnopqrstuvwx"
    ).json()["data"]["id"]
    response = client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SKILL_INVALID"


def test_offline_published_then_republish(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    offline = client.post(f"/api/skills/{skill_id}/offline", headers=auth_header(token))
    assert offline.status_code == 200
    assert offline.json()["data"]["status"] == "offline"

    republish = client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    assert republish.status_code == 200
    assert republish.json()["data"]["status"] == "published"


def test_offline_draft_conflicts(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    response = client.post(f"/api/skills/{skill_id}/offline", headers=auth_header(token))
    assert response.status_code == 409


def test_offline_twice_conflicts(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    client.post(f"/api/skills/{skill_id}/offline", headers=auth_header(token))
    response = client.post(f"/api/skills/{skill_id}/offline", headers=auth_header(token))
    assert response.status_code == 409


def test_state_transitions_require_ownership(client):
    owner, _ = register_expert(client)
    stranger, _ = register_expert(client, username="stranger3", email="stranger3@example.com")
    skill_id = create_skill(client, owner).json()["data"]["id"]
    assert (
        client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(stranger)).status_code
        == 403
    )
    assert (
        client.post(f"/api/skills/{skill_id}/offline", headers=auth_header(stranger)).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# validate 端点（不改状态）
# ---------------------------------------------------------------------------


def test_validate_saved_draft_reports_issues(client):
    token, _ = register_expert(client)
    skill_id = create_skill(
        client, token, constraints="约束里贴了密钥 sk-abcdefghijklmnopqrstuvwx"
    ).json()["data"]["id"]
    response = client.post(f"/api/skills/{skill_id}/validate", headers=auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["valid"] is False
    assert any(issue["rule"] == "api_key" for issue in data["issues"])

    detail = client.get(f"/api/skills/{skill_id}", headers=auth_header(token)).json()["data"]
    assert detail["status"] == "draft"


def test_validate_saved_published_skill_passes(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    response = client.post(f"/api/skills/{skill_id}/validate", headers=auth_header(token))
    assert response.json()["data"]["valid"] is True


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_delete_unbound_skill(client):
    token, _ = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]
    response = client.delete(f"/api/skills/{skill_id}", headers=auth_header(token))
    assert response.status_code == 200
    assert response.json()["data"]["message"] == "deleted"
    assert client.get(f"/api/skills/{skill_id}", headers=auth_header(token)).status_code == 404


def test_delete_bound_skill_conflicts(client, test_db):
    token, user_id = register_expert(client)
    skill_id = create_skill(client, token).json()["data"]["id"]

    async def seed_binding():
        async with test_db.session_factory() as session:
            expert = Expert(
                owner_id=user_id,
                name="周报专家",
                description="负责生成技术周报的专家",
                category="tech",
                persona="严谨的技术编辑",
                methodology="先收集再归纳",
                status="draft",
            )
            session.add(expert)
            await session.flush()
            session.add(ExpertSkill(expert_id=expert.id, skill_id=skill_id, enabled=False))
            await session.commit()

    test_db.run(seed_binding())

    response = client.delete(f"/api/skills/{skill_id}", headers=auth_header(token))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SKILL_STILL_BOUND"

    detail = client.get(f"/api/skills/{skill_id}", headers=auth_header(token))
    assert detail.status_code == 200


def test_delete_other_users_skill_forbidden(client):
    owner, _ = register_expert(client)
    stranger, _ = register_expert(client, username="stranger4", email="stranger4@example.com")
    skill_id = create_skill(client, owner).json()["data"]["id"]
    response = client.delete(f"/api/skills/{skill_id}", headers=auth_header(stranger))
    assert response.status_code == 403
