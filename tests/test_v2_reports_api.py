"""举报端点测试：限流 scope report（10/天，D12）+ 幂等 + 目标矩阵透传。"""

import pytest

from backend.v2.ids import uuid7
from tests.v2_content_helpers import seed_entity_with_revision
from tests.v2_provider_helpers import auth_client, login, seed_active_user


async def _user_client(pg, email: str):
    user_id = await seed_active_user(pg, email)
    client = auth_client()
    await login(client, email, "User-Passw0rd!")
    return client, user_id


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_report_endpoint_happy(pg, provider_env):
    author = await seed_active_user(pg, "rep-api-author@x.com")
    client, reporter = await _user_client(pg, "rep-api@x.com")
    _, revision_id = await seed_entity_with_revision(
        pg,
        author,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    resp = await client.post(
        "/api/reports",
        json={"target_type": "expert_revision", "target_id": revision_id, "reason": "内容违规"},
        headers={"Idempotency-Key": "rep-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "open"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_report_rate_limited_10_per_day(pg, provider_env):
    client, _ = await _user_client(pg, "rep-limit@x.com")
    # 限流先于目标校验（enforce 在任何业务查询前）：404 目标也计入窗口
    missing = str(uuid7())  # 合法 UUID → 服务层 404（非法 UUID 是 400，别混用）
    for i in range(10):
        resp = await client.post(
            "/api/reports",
            json={"target_type": "expert_revision", "target_id": missing, "reason": "x"},
            headers={"Idempotency-Key": f"rep-lim-{i}"},
        )
        assert resp.status_code == 404
    resp = await client.post(
        "/api/reports",
        json={"target_type": "expert_revision", "target_id": missing, "reason": "x"},
        headers={"Idempotency-Key": "rep-lim-over"},
    )
    assert resp.status_code == 429
    assert "retry-after" in {k.lower() for k in resp.headers.keys()}
    assert resp.json()["error"]["code"] == "TOO_MANY_REQUESTS"


@pytest.mark.usefixtures("provider_env")
@pytest.mark.asyncio
async def test_report_idempotent_replay(pg, provider_env):
    author = await seed_active_user(pg, "rep-rp-author@x.com")
    client, _ = await _user_client(pg, "rep-rp@x.com")
    _, revision_id = await seed_entity_with_revision(
        pg,
        author,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=True,
    )
    body = {"target_type": "expert_revision", "target_id": revision_id, "reason": "r"}
    first = await client.post("/api/reports", json=body, headers={"Idempotency-Key": "rep-rp-1"})
    replay = await client.post("/api/reports", json=body, headers={"Idempotency-Key": "rep-rp-1"})
    assert replay.json() == first.json()
