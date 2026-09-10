"""Provider 写面测试（T7 创建 / T9 更新撤销）。门序：登录 → CSRF → Idempotency-Key。"""

import json as _json
import uuid as _uuid

from sqlalchemy import text

from tests.v2_provider_helpers import (
    auth_client,
    catalog_id_by_host,
    login,
    seed_active_user,
    seed_provider,
    seed_task_for_provider,  # noqa: F401  # T9 更新撤销用例预载（helpers 共享面）
)

_CREATE = "/api/v2/providers"
_OPENAI_CID = None  # 每用例内经 catalog_id_by_host 取


async def _create(
    client,
    catalog_id: str,
    *,
    model_id="gpt-4o-mini",
    api_key="sk-test-abcdef123456",
    is_default=False,
    idem="idem-create-1",
):
    return await client.post(
        _CREATE,
        json={
            "catalog_id": catalog_id,
            "model_id": model_id,
            "api_key": api_key,
            "is_default": is_default,
        },
        headers={"Idempotency-Key": idem},
    )


async def test_create_provider_seals_and_persists(provider_env, pg):
    await seed_active_user(pg, "create@example.com")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    async with auth_client() as client:
        await login(client, "create@example.com", "User-Passw0rd!")
        resp = await _create(client, cid)
    assert resp.status_code == 200, resp.text
    row = resp.json()["data"]
    assert set(row.keys()) == {
        "id",
        "catalog_id",
        "catalog_display_name",
        "model_id",
        "key_last4",
        "key_version",
        "status",
        "is_default",
        "created_at",
    }
    assert row["key_last4"] == "3456" and row["key_version"] == 1 and row["status"] == "active"
    async with pg.engine.connect() as conn:
        db_row = (
            (
                await conn.execute(
                    text("SELECT key_ciphertext, dek_wrapped FROM user_providers WHERE id = :i"),
                    {"i": _uuid.UUID(row["id"])},
                )
            )
            .mappings()
            .one()
        )
    assert _json.loads(db_row["key_ciphertext"])["alg"] == "A256GCM"  # 双列均真实信封
    assert _json.loads(db_row["dek_wrapped"])["alg"] == "A256GCM"


async def test_create_rejects_base_url_extra_field(provider_env, pg):
    """禁 base_url：extra=forbid → 400（契约测试，裁决 D14）；零 DB 副作用。"""
    await seed_active_user(pg, "extra@example.com")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    async with auth_client() as client:
        await login(client, "extra@example.com", "User-Passw0rd!")
        resp = await client.post(
            _CREATE,
            json={
                "catalog_id": cid,
                "model_id": "gpt-4o-mini",
                "api_key": "sk-test-abcdef123456",
                "base_url": "https://evil.example",
            },
            headers={"Idempotency-Key": "idem-extra-1"},
        )
    assert resp.status_code == 400
    async with pg.engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM user_providers"))).scalar_one()
    assert n == 0


async def test_create_rejects_disabled_catalog_and_off_whitelist_model(provider_env, pg):
    uid = await seed_active_user(pg, "dis@example.com")
    faux = await catalog_id_by_host(pg, "faux.invalid")  # D16：种子恒 enabled=false
    deepseek = await catalog_id_by_host(pg, "api.deepseek.com")
    async with auth_client() as client:
        await login(client, "dis@example.com", "User-Passw0rd!")
        resp = await _create(client, faux, model_id="faux-echo", idem="idem-dis-1")
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "CATALOG_ITEM_DISABLED"
        resp = await _create(client, deepseek, model_id="gpt-4o", idem="idem-dis-2")
        assert resp.status_code == 400 and resp.json()["error"]["code"] == "MODEL_NOT_ALLOWED"
    del uid


async def test_create_duplicate_active_409(provider_env, pg):
    uid = await seed_active_user(pg, "dup@example.com")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    async with auth_client() as client:
        await login(client, "dup@example.com", "User-Passw0rd!")
        first = await _create(client, cid, idem="idem-dup-1")
        assert first.status_code == 200
        second = await _create(client, cid, idem="idem-dup-2")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "PROVIDER_DUPLICATE"
    del uid


async def test_create_revoked_then_readd_succeeds(provider_env, pg):
    """D4：软撤行不参与唯一索引——同条目重添加可行（新行 key_version=1）。"""
    uid = await seed_active_user(pg, "readd@example.com")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    async with auth_client() as client:
        await login(client, "readd@example.com", "User-Passw0rd!")
        await _create(client, cid, idem="idem-re-1")
        # T9 前用 superuser 直接置 revoked 模拟软撤
        async with pg.engine.begin() as conn:
            await conn.execute(
                text("UPDATE user_providers SET status='revoked' WHERE user_id=:u"), {"u": uid}
            )
        second = await _create(client, cid, idem="idem-re-2")
    assert second.status_code == 200
    assert second.json()["data"]["key_version"] == 1


async def test_create_default_switches_default(provider_env, pg):
    """D5：置默认先清旧默认；全用户唯一默认成立。"""
    uid = await seed_active_user(pg, "def@example.com")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    # 旧默认须为异条目（不同 model_id）：D4 重复门先于默认互斥——同 (catalog, model)
    # 的旧默认会命中 409 而非走默认切换路径。
    old = await seed_provider(pg, uid, is_default=True, model_id="gpt-4o")
    async with auth_client() as client:
        await login(client, "def@example.com", "User-Passw0rd!")
        resp = await _create(client, cid, is_default=True, idem="idem-def-1")
    assert resp.status_code == 200
    async with pg.engine.connect() as conn:
        defaults = (
            (
                await conn.execute(
                    text("SELECT id FROM user_providers WHERE user_id=:u AND is_default=true"),
                    {"u": uid},
                )
            )
            .scalars()
            .all()
        )
    assert defaults == [_uuid.UUID(resp.json()["data"]["id"])] and old not in defaults


async def test_create_replay_returns_original(provider_env, pg):
    """幂等命中 → 原响应重放；不改状态。"""
    await seed_active_user(pg, "replay@example.com")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    async with auth_client() as client:
        await login(client, "replay@example.com", "User-Passw0rd!")
        first = await _create(client, cid, idem="idem-rp-1")
        replay = await _create(client, cid, idem="idem-rp-1")
    assert replay.status_code == first.status_code
    assert replay.json() == first.json()
    async with pg.engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM user_providers"))).scalar_one()
    assert n == 1


async def test_create_requires_idempotency_key(provider_env, pg):
    await seed_active_user(pg, "nokey@example.com")
    cid = await catalog_id_by_host(pg, "api.openai.com")
    async with auth_client() as client:
        await login(client, "nokey@example.com", "User-Passw0rd!")
        resp = await client.post(
            _CREATE,
            json={"catalog_id": cid, "model_id": "gpt-4o-mini", "api_key": "sk-test-abcdef123456"},
        )
    assert resp.status_code == 400  # require_key_header


async def test_create_unknown_catalog_400(provider_env, pg):
    await seed_active_user(pg, "nocat@example.com")
    async with auth_client() as client:
        await login(client, "nocat@example.com", "User-Passw0rd!")
        resp = await _create(client, "0197aaaa-7aaa-7aaa-7aaa-aaaaaaaaaaaa", idem="idem-nc-1")
    assert resp.status_code == 400


async def test_create_bad_catalog_uuid_400(provider_env, pg):
    await seed_active_user(pg, "badcat@example.com")
    async with auth_client() as client:
        await login(client, "badcat@example.com", "User-Passw0rd!")
        resp = await _create(client, "not-a-uuid", idem="idem-nc-2")
    assert resp.status_code == 400


def test_map_integrity_conflict_by_constraint_name():
    """D5：IntegrityError 按约束名分流（并发默认互斥 vs 条目重复）——文案契约锁定。"""
    from sqlalchemy.exc import IntegrityError as _IE

    from backend.v2 import provider_service

    default_err = _IE(
        'duplicate key value violates unique constraint "uq_user_providers_one_default"',
        None,
        Exception(),
    )
    entry_err = _IE(
        'duplicate key value violates unique constraint "uq_user_providers_active_entry"',
        None,
        Exception(),
    )
    assert provider_service._map_integrity_conflict(default_err).message == (
        provider_service._DEFAULT_CONFLICT_MESSAGE
    )
    assert provider_service._map_integrity_conflict(entry_err).message == (
        provider_service._DUPLICATE_MESSAGE
    )
