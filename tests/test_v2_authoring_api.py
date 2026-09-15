"""作者面 HTTP 端点测试（真实登录流 + CSRF + Idempotency-Key；D6 端点契约）。"""

import uuid

import pytest

from tests.v2_content_helpers import (
    EXPERT_CONTENT,
    SKILL_CONTENT,
    seed_entitlement,
    seed_entity_with_revision,
)
from tests.v2_provider_helpers import auth_client, login, seed_active_user


async def _author_client(pg, email: str):
    """种子 expert_author 用户并真实登录，返回 (client, user_id)。"""
    user_id = await seed_active_user(pg, email)
    await seed_entitlement(pg, user_id)
    client = auth_client()
    await login(client, email, "User-Passw0rd!")
    return client, user_id


# ---------- 核心用例（brief Step 2 全文）----------


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_create_expert_endpoint(pg, provider_env):
    client, _ = await _author_client(pg, "api-author@x.com")
    resp = await client.post(
        "/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "ep-1"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["entity"]["status"] == "draft"
    assert body["revision"]["revision_no"] == 1


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_create_expert_replay_without_set_cookie(pg, provider_env):
    client, _ = await _author_client(pg, "api-replay@x.com")
    first = await client.post(
        "/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "ep-1"}
    )
    replay = await client.post(
        "/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "ep-1"}
    )
    assert replay.status_code == first.status_code
    assert replay.json() == first.json()
    assert "set-cookie" not in {k.lower() for k in replay.headers.keys()}


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_create_requires_idempotency_key(pg, provider_env):
    client, _ = await _author_client(pg, "api-nokey@x.com")
    resp = await client.post("/api/v2/experts", json=EXPERT_CONTENT)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_create_requires_csrf(pg, provider_env):
    user_id = await seed_active_user(pg, "api-nocsrf@x.com")
    await seed_entitlement(pg, user_id)
    client = auth_client()
    await login(client, "api-nocsrf@x.com", "User-Passw0rd!")
    client.headers.pop("X-CSRF-Token")  # 摘掉 CSRF
    resp = await client.post(
        "/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "ep-1"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_INVALID"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_full_flow_create_edit_submit(pg, provider_env):
    client, _ = await _author_client(pg, "api-flow@x.com")
    created = (
        await client.post("/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "f1"})
    ).json()["data"]
    entity_id = created["entity"]["id"]
    revision_id = created["revision"]["revision_id"]
    # PUT：最新为 draft → 覆写
    edited = dict(EXPERT_CONTENT, persona="流程测试 persona。")
    put = await client.put(
        f"/api/v2/experts/{entity_id}", json=edited, headers={"Idempotency-Key": "f2"}
    )
    assert put.status_code == 200
    assert put.json()["data"]["revision"]["revision_id"] == revision_id
    # submit（harness-kind 自 T6 起提审 400——用 container-kind 工具）
    submitted = await client.post(
        f"/api/v2/experts/{entity_id}/revisions/{revision_id}/submit",
        json={"tools": [{"tool_id": "read_task_file", "version": "1"}]},
        headers={"Idempotency-Key": "f3"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["revision"]["status"] == "pending_review"
    # 详情可见 revision 状态
    detail = await client.get(f"/api/v2/experts/{entity_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["revisions"][0]["status"] == "pending_review"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_without_entitlement_403(pg, provider_env):
    await seed_active_user(pg, "api-noent@x.com")
    client = auth_client()
    await login(client, "api-noent@x.com", "User-Passw0rd!")
    resp = await client.post(
        "/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "e1"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_get_cross_owner_404(pg, provider_env):
    client_a, _ = await _author_client(pg, "api-own@x.com")
    created = (
        await client_a.post(
            "/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "o1"}
        )
    ).json()["data"]
    client_b, _ = await _author_client(pg, "api-other@x.com")
    resp = await client_b.get(f"/api/v2/experts/{created['entity']['id']}")
    assert resp.status_code == 404


# ---------- 镜像用例（brief 镜像用例表；形态照抄核心用例）----------


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_expert_validation_error_400(pg, provider_env):
    """Pydantic 非空白拒绝 → RequestValidationError 经 main.py 统一 handler → 400 信封。"""
    client, _ = await _author_client(pg, "api-badval@x.com")
    resp = await client.post(
        "/api/v2/experts",
        json=dict(EXPERT_CONTENT, persona="   "),
        headers={"Idempotency-Key": "v1"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_expert_unknown_field_400(pg, provider_env):
    """extra=forbid → 400（同 RequestValidationError handler）；零 DB 副作用。"""
    client, _ = await _author_client(pg, "api-extra@x.com")
    resp = await client.post(
        "/api/v2/experts", json=dict(EXPERT_CONTENT, hacker=1), headers={"Idempotency-Key": "v2"}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_skill_full_flow(pg, provider_env):
    """skill 全链 create→submit(tools=[]) → 200 pending_review；skill 传 tools → 400。"""
    client, _ = await _author_client(pg, "api-skill@x.com")
    created = (
        await client.post("/api/v2/skills", json=SKILL_CONTENT, headers={"Idempotency-Key": "s1"})
    ).json()["data"]
    assert created["entity"]["status"] == "draft"
    entity_id = created["entity"]["id"]
    revision_id = created["revision"]["revision_id"]
    submitted = await client.post(
        f"/api/v2/skills/{entity_id}/revisions/{revision_id}/submit",
        json={"tools": []},
        headers={"Idempotency-Key": "s2"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["revision"]["status"] == "pending_review"
    # skill 域传 tools → 400（_normalize_tools allow=False，先于状态门）
    with_tools = await client.post(
        f"/api/v2/skills/{entity_id}/revisions/{revision_id}/submit",
        json={"tools": [{"tool_id": "check_code_style", "version": "1"}]},
        headers={"Idempotency-Key": "s3"},
    )
    assert with_tools.status_code == 400
    assert with_tools.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_list_status_filter_endpoint(pg, provider_env):
    """HTTP 造 draft + superuser 造 published → ?status=draft 只含 draft 项。"""
    client, user_id = await _author_client(pg, "api-list@x.com")
    created = (
        await client.post("/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "l1"})
    ).json()["data"]
    await seed_entity_with_revision(
        pg,
        user_id,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    resp = await client.get("/api/v2/experts", params={"status": "draft"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]
    assert [i["id"] for i in items] == [created["entity"]["id"]]
    assert items[0]["status"] == "draft"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_get_invalid_uuid_400(pg, provider_env):
    client, _ = await _author_client(pg, "api-baduuid@x.com")
    resp = await client.get("/api/v2/experts/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_submit_unknown_tool_400_endpoint(pg, provider_env):
    """tools 条目不在 tool_catalog → 400 VALIDATION_ERROR。"""
    client, _ = await _author_client(pg, "api-badtool@x.com")
    created = (
        await client.post("/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "t1"})
    ).json()["data"]
    resp = await client.post(
        f"/api/v2/experts/{created['entity']['id']}"
        f"/revisions/{created['revision']['revision_id']}/submit",
        json={"tools": [{"tool_id": "nope", "version": "1"}]},
        headers={"Idempotency-Key": "t2"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_submit_idempotent_replay(pg, provider_env):
    """同 key 两次 submit：第二次响应与首次全等，无 set-cookie。"""
    client, _ = await _author_client(pg, "api-subrp@x.com")
    created = (
        await client.post("/api/v2/experts", json=EXPERT_CONTENT, headers={"Idempotency-Key": "r1"})
    ).json()["data"]
    submit_path = (
        f"/api/v2/experts/{created['entity']['id']}"
        f"/revisions/{created['revision']['revision_id']}/submit"
    )
    first = await client.post(submit_path, json={"tools": []}, headers={"Idempotency-Key": "r2"})
    replay = await client.post(submit_path, json={"tools": []}, headers={"Idempotency-Key": "r2"})
    assert first.status_code == 200, first.text
    assert replay.status_code == first.status_code
    assert replay.json() == first.json()
    assert "set-cookie" not in {k.lower() for k in replay.headers.keys()}


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_skill_detail_shape(pg, provider_env):
    """skill 详情出参键形态与 experts 同构（{"skill": {...}, "revisions": [...]}）。"""
    client, _ = await _author_client(pg, "api-skilldetail@x.com")
    created = (
        await client.post("/api/v2/skills", json=SKILL_CONTENT, headers={"Idempotency-Key": "d1"})
    ).json()["data"]
    detail = await client.get(f"/api/v2/skills/{created['entity']['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()["data"]
    assert set(body.keys()) == {"skill", "revisions"}
    assert uuid.UUID(body["skill"]["id"]) == uuid.UUID(created["entity"]["id"])
    assert body["revisions"][0]["revision_id"] == created["revision"]["revision_id"]
