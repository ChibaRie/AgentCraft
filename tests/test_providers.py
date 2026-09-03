"""Provider CRUD API 测试（P10 BYOK，DB 设计 §3.12 / 手册 §7.7 双模式）。

- Key 仅写入：响应永不含密文/明文，只回 api_key_hint（尾 4 位掩码）
- 信封加密 AAD 绑定归属用户（agentcraft:user_providers:{user_id}:api_key:v1）
- is_default 用户内单选（事务保证）
- 回退链在任务创建侧测试（test_tasks_provider.py）；本文件管配置本身
"""

import base64
import json
import os

import pytest

from backend.config import Settings, get_settings
from backend.main import app
from backend.utils.crypto import EncryptionError, decrypt_text, make_keyring, provider_key_aad
from tests.test_skills import auth_header, register_expert

pytestmark = pytest.mark.usefixtures("client")


@pytest.fixture()
def crypto_settings():
    """带信封密钥环的设置覆盖（Provider Key 加密需要）。"""
    raw = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    settings = Settings(
        MCP_ENCRYPTION_ACTIVE_KID="primary",
        MCP_ENCRYPTION_KEYRING=f"primary:{raw}",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    yield settings
    app.dependency_overrides.pop(get_settings, None)


def create_provider(client, token, **overrides):
    payload = {
        "name": "DeepSeek 主力",
        "protocol": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-test-abcd1234",
        "model_id": "deepseek-chat",
        "is_default": False,
        **overrides,
    }
    return client.post("/api/providers", json=payload, headers=auth_header(token))


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------


def test_providers_require_authentication(client):
    assert client.get("/api/providers").status_code == 401
    assert client.post("/api/providers", json={}).status_code == 401
    assert client.get("/api/providers/1").status_code == 401
    assert client.put("/api/providers/1", json={}).status_code == 401
    assert client.delete("/api/providers/1").status_code == 401


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------


def test_create_provider_happy_path(client, crypto_settings):
    token, _ = register_expert(client, username="prov-a", email="prov-a@example.com")
    response = create_provider(client, token, is_default=True)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["id"] > 0
    assert data["name"] == "DeepSeek 主力"
    assert data["protocol"] == "openai"
    assert data["base_url"] == "https://api.deepseek.com/v1"
    assert data["model_id"] == "deepseek-chat"
    assert data["is_default"] is True
    # Key 只回掩码
    assert data["api_key_hint"] == "****1234"
    serialized = json.dumps(response.json())
    assert "sk-test" not in serialized and "ciphertext" not in serialized


def test_create_provider_without_key(client, crypto_settings):
    """本机免 Key 端点（如 Ollama）：api_key 缺省合法。"""
    token, _ = register_expert(client, username="prov-b", email="prov-b@example.com")
    response = create_provider(client, token, name="本地 Ollama", api_key=None)
    assert response.status_code == 201
    assert response.json()["data"]["api_key_hint"] is None


def test_create_provider_name_conflict(client, crypto_settings):
    token, _ = register_expert(client, username="prov-c", email="prov-c@example.com")
    assert create_provider(client, token).status_code == 201
    conflict = create_provider(client, token, base_url="https://other.example.com/v1")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "PROVIDER_NAME_EXISTS"


def test_create_provider_validation_errors(client, crypto_settings):
    token, _ = register_expert(client, username="prov-d", email="prov-d@example.com")
    for payload in (
        {"name": "短"},  # <2 字符
        {"base_url": "ftp://x.example.com"},  # 非 http(s)
        {"model_id": "  "},  # 空白
        {"protocol": "anthropic"},  # v1 枚举未开放
    ):
        response = create_provider(client, token, **payload)
        assert response.status_code == 400, f"payload={payload}"


def test_create_provider_encryption_unconfigured(client):
    """未配置密钥环时写 Key → 503（读/免 Key 配置不受影响）。"""
    token, _ = register_expert(client, username="prov-e", email="prov-e@example.com")
    response = create_provider(client, token)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ENCRYPTION_UNCONFIGURED"
    # 免 Key 配置仍可创建
    ok = create_provider(client, token, name="免钥", api_key=None)
    assert ok.status_code == 201


# ---------------------------------------------------------------------------
# 查询（所有权）
# ---------------------------------------------------------------------------


def test_list_providers_own_only(client, crypto_settings):
    token_a, _ = register_expert(client, username="prov-f", email="prov-f@example.com")
    token_b, _ = register_expert(client, username="prov-g", email="prov-g@example.com")
    create_provider(client, token_a, name="A 的配置")
    create_provider(client, token_b, name="B 的配置")
    listed = client.get("/api/providers", headers=auth_header(token_a))
    assert listed.status_code == 200
    names = [item["name"] for item in listed.json()["data"]]
    assert names == ["A 的配置"]


def test_get_provider_ownership(client, crypto_settings):
    token_a, _ = register_expert(client, username="prov-h", email="prov-h@example.com")
    token_b, _ = register_expert(client, username="prov-i", email="prov-i@example.com")
    provider_id = create_provider(client, token_a).json()["data"]["id"]
    own = client.get(f"/api/providers/{provider_id}", headers=auth_header(token_a))
    assert own.status_code == 200
    other = client.get(f"/api/providers/{provider_id}", headers=auth_header(token_b))
    assert other.status_code == 403
    missing = client.get("/api/providers/99999", headers=auth_header(token_a))
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# 更新（部分语义）
# ---------------------------------------------------------------------------


def test_update_provider_partial(client, crypto_settings, test_db):
    token, user_id = register_expert(client, username="prov-j", email="prov-j@example.com")
    provider_id = create_provider(client, token).json()["data"]["id"]

    # 只改 model_id：Key 不动（hint 不变）
    updated = client.put(
        f"/api/providers/{provider_id}",
        json={"model_id": "deepseek-reasoner"},
        headers=auth_header(token),
    )
    assert updated.status_code == 200
    data = updated.json()["data"]
    assert data["model_id"] == "deepseek-reasoner"
    assert data["api_key_hint"] == "****1234"

    # 换 Key：hint 跟随
    rekeyed = client.put(
        f"/api/providers/{provider_id}",
        json={"api_key": "sk-new-9999zzzz"},
        headers=auth_header(token),
    )
    assert rekeyed.json()["data"]["api_key_hint"] == "****zzzz"

    # 显式 null：清除 Key（免 Key 端点）
    cleared = client.put(
        f"/api/providers/{provider_id}",
        json={"api_key": None},
        headers=auth_header(token),
    )
    assert cleared.json()["data"]["api_key_hint"] is None

    # 落库确证：密文可按归属用户 AAD 解密
    async def load_row():
        from sqlalchemy import select

        from backend.models.user_provider import UserProvider

        async with test_db.session_factory() as session:
            row = (
                await session.execute(
                    select(UserProvider).where(UserProvider.id == provider_id)
                )
            ).scalar_one()
            return row

    row = asyncio_run(load_row())
    assert row.api_key_encrypted is None


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def test_update_provider_ciphertext_roundtrip(client, crypto_settings, test_db):
    token, user_id = register_expert(client, username="prov-k", email="prov-k@example.com")
    provider_id = create_provider(client, token, api_key="sk-roundtrip-8888").json()["data"]["id"]

    async def load_row():
        from sqlalchemy import select

        from backend.models.user_provider import UserProvider

        async with test_db.session_factory() as session:
            return (
                await session.execute(
                    select(UserProvider).where(UserProvider.id == provider_id)
                )
            ).scalar_one()

    row = asyncio_run(load_row())
    envelope = json.loads(row.api_key_encrypted)
    plaintext = decrypt_text(
        envelope,
        aad=provider_key_aad(user_id),
        keyring=make_keyring(crypto_settings.MCP_ENCRYPTION_KEYRING)[1],
    )
    assert plaintext == "sk-roundtrip-8888"
    # 换用户 AAD 解密必须失败（归属绑定）
    _, keyring = make_keyring(crypto_settings.MCP_ENCRYPTION_KEYRING)
    with pytest.raises(EncryptionError):
        decrypt_text(envelope, aad=provider_key_aad(user_id + 1), keyring=keyring)


def test_set_default_exclusive(client, crypto_settings):
    token, _ = register_expert(client, username="prov-l", email="prov-l@example.com")
    first = create_provider(client, token, name="其一", is_default=True).json()["data"]["id"]
    second = create_provider(client, token, name="其二", is_default=True).json()["data"]["id"]
    listed = client.get("/api/providers", headers=auth_header(token)).json()["data"]
    by_id = {item["id"]: item["is_default"] for item in listed}
    assert by_id[first] is False and by_id[second] is True
    # 显式取消默认（允许无默认状态）
    unset = client.put(
        f"/api/providers/{second}", json={"is_default": False}, headers=auth_header(token)
    )
    assert unset.status_code == 200
    listed = client.get("/api/providers", headers=auth_header(token)).json()["data"]
    assert all(item["is_default"] is False for item in listed)


# ---------------------------------------------------------------------------
# 删除（快照自足：删除不阻塞既有任务）
# ---------------------------------------------------------------------------


def test_delete_provider(client, crypto_settings):
    token, _ = register_expert(client, username="prov-m", email="prov-m@example.com")
    provider_id = create_provider(client, token).json()["data"]["id"]
    deleted = client.delete(f"/api/providers/{provider_id}", headers=auth_header(token))
    assert deleted.status_code == 200
    gone = client.get(f"/api/providers/{provider_id}", headers=auth_header(token))
    assert gone.status_code == 404
    # 他人配置不可删
    other_id = create_provider(client, token, name="他人的").json()["data"]["id"]
    token_b, _ = register_expert(client, username="prov-n", email="prov-n@example.com")
    forbidden = client.delete(f"/api/providers/{other_id}", headers=auth_header(token_b))
    assert forbidden.status_code == 403
