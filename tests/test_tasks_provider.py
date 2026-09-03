"""任务创建的 Provider 选择与快照冻结测试（§7.7 BYOK / DB 设计 §3.5）。

- 回退链：显式 provider_config_id → 用户 is_default → 系统默认（source=system）
- 快照冻结：source/protocol/base_url/model_id + Key 信封密文（user 来源）；
  创建后改配置/换默认不影响已建任务
- 校验：他人/不存在的 provider_config_id → 404
- 详情暴露 Provider 摘要（不含任何 Key 形态）
"""

import base64
import json
import os

import pytest

from backend.config import Settings, get_settings
from backend.main import app
from tests.test_experts import valid_expert
from tests.test_providers import create_provider
from tests.test_skills import auth_header, register_expert

pytestmark = pytest.mark.usefixtures("client")


@pytest.fixture()
def crypto_settings():
    raw = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    settings = Settings(
        MCP_ENCRYPTION_ACTIVE_KID="primary",
        MCP_ENCRYPTION_KEYRING=f"primary:{raw}",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    yield settings
    app.dependency_overrides.pop(get_settings, None)


def make_task_owner(client, username="prov-tasker"):
    """注册 → 建 Skill（发布）→ 建专家 → 绑定启用 → 发布，返回 (token, expert_id)。"""
    token, _ = register_expert(client, username=username, email=f"{username}@example.com")
    skill = client.post(
        "/api/skills",
        json={
            "name": "验收整理术",
            "description": "任务 Provider 快照验收技能",
            "use_case": "Provider 快照冻结行为的端到端验证场景",
            "role": "结构化整理素材的验收专家",
            "goal": "产出结构化、可逐字段断言的 Provider 配置快照",
            "steps": "创建任务 → 断言快照 → 修改配置 → 断言快照不变",
            "output_requirements": "输出结构化的 Provider 快照断言结果",
            "constraints": "不得编造任何未提供的配置字段，引用必须注明来源",
        },
        headers=auth_header(token),
    )
    assert skill.status_code == 201, skill.text
    skill_id = skill.json()["data"]["id"]
    client.post(f"/api/skills/{skill_id}/publish", headers=auth_header(token))
    expert = client.post(
        "/api/experts", json=valid_expert(), headers=auth_header(token)
    )
    expert_id = expert.json()["data"]["id"]
    client.post(
        f"/api/experts/{expert_id}/skills", json={"skill_id": skill_id}, headers=auth_header(token)
    )
    client.put(
        f"/api/experts/{expert_id}/skills/{skill_id}",
        json={"enabled": True},
        headers=auth_header(token),
    )
    client.post(f"/api/experts/{expert_id}/publish", headers=auth_header(token))
    return token, expert_id


def create_task(client, token, expert_id, **overrides):
    payload = {"expert_id": expert_id, "description": "Provider 快照验收", **overrides}
    return client.post("/api/tasks", json=payload, headers=auth_header(token))


def test_task_without_provider_falls_back_to_system(client):
    """未配置任何 Provider：source=system（现行为不变；显式注入系统默认保证封闭）。"""
    from backend.config import Settings as _Settings

    settings = _Settings(
        PI_PROVIDER="openai",
        PI_MODEL="gpt-4o-mini",
        PI_PROXY_BASE_URL="http://provider-proxy:8080/v1",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    token, expert_id = make_task_owner(client)
    response = create_task(client, token, expert_id)
    assert response.status_code == 201
    detail = client.get(
        f"/api/tasks/{response.json()['data']['task_id']}", headers=auth_header(token)
    ).json()["data"]
    provider = detail["provider"]
    assert provider["source"] == "system"
    assert provider["protocol"] == "openai"
    assert provider["base_url"] == "http://provider-proxy:8080/v1"
    assert provider["model_id"] == "gpt-4o-mini"
    assert provider["api_key_set"] is False
    assert "encrypted" not in json.dumps(provider) and "sk-" not in json.dumps(provider)
    app.dependency_overrides.pop(get_settings, None)


def test_task_with_explicit_provider_snapshots_user_config(client, crypto_settings):
    token, expert_id = make_task_owner(client)
    provider_id = create_provider(
        client,
        token,
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        model_id="deepseek-chat",
    ).json()["data"]["id"]
    response = create_task(client, token, expert_id, provider_config_id=provider_id)
    assert response.status_code == 201
    detail = client.get(
        f"/api/tasks/{response.json()['data']['task_id']}", headers=auth_header(token)
    ).json()["data"]
    provider = detail["provider"]
    assert provider["source"] == "user"
    assert provider["user_provider_id"] == provider_id
    assert provider["base_url"] == "https://api.deepseek.com/v1"
    assert provider["model_id"] == "deepseek-chat"
    assert provider["api_key_set"] is True
    # 任何 Key 形态不得出现在响应
    assert "sk-" not in json.dumps(detail)


def test_task_falls_back_to_user_default(client, crypto_settings):
    token, expert_id = make_task_owner(client)
    create_provider(
        client,
        token,
        name="默认源",
        base_url="https://api.default.example.com/v1",
        model_id="default-model",
        is_default=True,
    )
    response = create_task(client, token, expert_id)
    detail = client.get(
        f"/api/tasks/{response.json()['data']['task_id']}", headers=auth_header(token)
    ).json()["data"]
    assert detail["provider"]["source"] == "user"
    assert detail["provider"]["base_url"] == "https://api.default.example.com/v1"


def test_task_rejects_foreign_provider(client, crypto_settings):
    token, expert_id = make_task_owner(client, username="prov-owner")
    token_b, _ = register_expert(
        client, username="prov-stranger", email="prov-stranger@example.com"
    )
    foreign_id = create_provider(client, token_b, name="他人的").json()["data"]["id"]
    response = create_task(client, token, expert_id, provider_config_id=foreign_id)
    assert response.status_code == 404  # 不暴露他人配置存在性
    missing = create_task(client, token, expert_id, provider_config_id=99999)
    assert missing.status_code == 404


def test_provider_snapshot_frozen_after_config_change(client, crypto_settings, test_db):
    """冻结验证：创建后改配置/换默认/删除配置，已建任务的快照不变。"""

    from backend.models.task import Task

    token, expert_id = make_task_owner(client)
    provider_id = create_provider(
        client,
        token,
        base_url="https://api.freeze.example.com/v1",
        model_id="freeze-model",
    ).json()["data"]["id"]
    task_id = create_task(client, token, expert_id, provider_config_id=provider_id).json()["data"][
        "task_id"
    ]

    async def load_snapshot():
        async with test_db.session_factory() as session:
            row = await session.get(Task, task_id)
            return row.provider_snapshot

    before = asyncio_run(load_snapshot())
    assert json.loads(before)["base_url"] == "https://api.freeze.example.com/v1"
    assert json.loads(before)["api_key_encrypted"] is not None  # 信封密文已入库

    # 改配置 + 换默认 + 删除原配置
    client.put(
        f"/api/providers/{provider_id}",
        json={"base_url": "https://changed.example.com/v1", "is_default": True},
        headers=auth_header(token),
    )
    client.delete(f"/api/providers/{provider_id}", headers=auth_header(token))

    after = asyncio_run(load_snapshot())
    assert after == before, "快照必须与创建时点逐字节一致"


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
