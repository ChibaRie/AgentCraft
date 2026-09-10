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


# ---------- T9 PUT/DELETE ----------


async def test_put_rotate_key_bumps_version_and_fails_unstarted(provider_env, pg):
    """轮换：新 DEK 覆写 + key_version+1 + 同事务联动未开始任务（D2/D3）。"""
    uid = await seed_active_user(pg, "rot@example.com")
    pid = await seed_provider(pg, uid)
    await seed_task_for_provider(pg, uid, pid, status="queued")
    async with auth_client() as client:
        await login(client, "rot@example.com", "User-Passw0rd!")
        resp = await client.put(
            f"/api/v2/providers/{pid}",
            json={"api_key": "sk-rotated-key-987654"},
            headers={"Idempotency-Key": "idem-rot-1"},
        )
    assert resp.status_code == 200, resp.text
    row = resp.json()["data"]
    # "sk-rotated-key-987654"[-4:]（D7 末 4 位原样）
    assert row["key_version"] == 2 and row["key_last4"] == "7654"
    async with pg.engine.connect() as conn:
        task = (
            await conn.execute(
                text("SELECT status, abort_reason FROM tasks WHERE provider_id = :p"),
                {"p": str(pid)},
            )
        ).one()
        n_ct = (await conn.execute(text("SELECT count(*) FROM user_providers"))).scalar_one()
    assert task.status == "failed" and task.abort_reason == "provider_key_revoked"
    assert n_ct == 1  # 覆写非新增


async def test_put_null_api_key_rejected(provider_env, pg):
    """裁决 D2：显式 null → 400 固定文案；无状态变更（不轮换）。"""
    uid = await seed_active_user(pg, "null@example.com")
    pid = await seed_provider(pg, uid)
    async with auth_client() as client:
        await login(client, "null@example.com", "User-Passw0rd!")
        resp = await client.put(
            f"/api/v2/providers/{pid}",
            json={"api_key": None},
            headers={"Idempotency-Key": "idem-null-1"},
        )
    assert resp.status_code == 400
    assert "不支持置空" in resp.json()["error"]["message"]
    async with pg.engine.connect() as conn:
        v = (
            await conn.execute(
                text("SELECT key_version FROM user_providers WHERE id=:i"), {"i": pid}
            )
        ).scalar_one()
    assert v == 1


async def test_put_null_model_id_rejected(provider_env, pg):
    """LOW-1 语义门：显式 null model_id → 400 VALIDATION_ERROR；行不变（缺席≠null）。"""
    uid = await seed_active_user(pg, "nullmdl@example.com")
    pid = await seed_provider(pg, uid)
    async with auth_client() as client:
        await login(client, "nullmdl@example.com", "User-Passw0rd!")
        resp = await client.put(
            f"/api/v2/providers/{pid}",
            json={"model_id": None},
            headers={"Idempotency-Key": "idem-null-m1"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "不支持置空" in resp.json()["error"]["message"]
    async with pg.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT model_id, key_version FROM user_providers WHERE id=:i"), {"i": pid}
            )
        ).one()
    assert row.model_id == "gpt-4o-mini" and row.key_version == 1


async def test_put_null_is_default_rejected(provider_env, pg):
    """LOW-1 语义门：显式 null is_default → 400 VALIDATION_ERROR（缺席=不变，布尔=覆盖）。"""
    uid = await seed_active_user(pg, "nulldf@example.com")
    pid = await seed_provider(pg, uid)
    async with auth_client() as client:
        await login(client, "nulldf@example.com", "User-Passw0rd!")
        resp = await client.put(
            f"/api/v2/providers/{pid}",
            json={"is_default": None},
            headers={"Idempotency-Key": "idem-null-d1"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    async with pg.engine.connect() as conn:
        d = (
            await conn.execute(
                text("SELECT is_default FROM user_providers WHERE id=:i"), {"i": pid}
            )
        ).scalar_one()
    assert d is False


async def test_put_absent_api_key_unchanged(provider_env, pg):
    """缺席=不变：仅 is_default 变更，key_version 不动。"""
    uid = await seed_active_user(pg, "keep@example.com")
    pid = await seed_provider(pg, uid)
    async with auth_client() as client:
        await login(client, "keep@example.com", "User-Passw0rd!")
        resp = await client.put(
            f"/api/v2/providers/{pid}",
            json={"is_default": True},
            headers={"Idempotency-Key": "idem-keep-1"},
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["key_version"] == 1
    del uid


async def test_put_model_id_whitelist_enforced(provider_env, pg):
    uid = await seed_active_user(pg, "mdl@example.com")
    pid = await seed_provider(pg, uid, catalog_host="api.openai.com")
    async with auth_client() as client:
        await login(client, "mdl@example.com", "User-Passw0rd!")
        ok = await client.put(
            f"/api/v2/providers/{pid}",
            json={"model_id": "gpt-4o"},
            headers={"Idempotency-Key": "idem-m1"},
        )
        bad = await client.put(
            f"/api/v2/providers/{pid}",
            json={"model_id": "deepseek-chat"},
            headers={"Idempotency-Key": "idem-m2"},
        )
    assert ok.status_code == 200 and ok.json()["data"]["model_id"] == "gpt-4o"
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "MODEL_NOT_ALLOWED"


async def test_put_extra_forbid(provider_env, pg):
    """PUT 也禁 base_url（D14）。"""
    uid = await seed_active_user(pg, "putx@example.com")
    pid = await seed_provider(pg, uid)
    async with auth_client() as client:
        await login(client, "putx@example.com", "User-Passw0rd!")
        resp = await client.put(
            f"/api/v2/providers/{pid}",
            json={"base_url": "https://evil.example"},
            headers={"Idempotency-Key": "idem-px-1"},
        )
    assert resp.status_code == 400


async def test_put_cross_user_and_revoked_404(provider_env, pg):
    uid_a = await seed_active_user(pg, "owner@example.com")
    uid_b = await seed_active_user(pg, "intruder@example.com")
    pid = await seed_provider(pg, uid_a)
    pid_revoked = await seed_provider(pg, uid_b, status="revoked")
    async with auth_client() as client:
        await login(client, "intruder@example.com", "User-Passw0rd!")
        cross = await client.put(
            f"/api/v2/providers/{pid}",
            json={"is_default": True},
            headers={"Idempotency-Key": "idem-x1"},
        )
        gone = await client.put(
            f"/api/v2/providers/{pid_revoked}", json={}, headers={"Idempotency-Key": "idem-x2"}
        )
    assert cross.status_code == 404 and gone.status_code == 404  # RLS 0 行统一 404；revoked 不可见
    del uid_a


async def test_delete_revokes_clears_default_and_fails_unstarted(provider_env, pg):
    """D6：默认行撤销 → is_default 同事务清除；联动未开始任务；revoked 后 404。"""
    uid = await seed_active_user(pg, "del@example.com")
    pid = await seed_provider(pg, uid, is_default=True)
    await seed_task_for_provider(pg, uid, pid, status="uploading")
    async with auth_client() as client:
        await login(client, "del@example.com", "User-Passw0rd!")
        resp = await client.delete(
            f"/api/v2/providers/{pid}", headers={"Idempotency-Key": "idem-del-1"}
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == {"id": str(pid), "status": "revoked"}
        again = await client.delete(
            f"/api/v2/providers/{pid}", headers={"Idempotency-Key": "idem-del-2"}
        )
    assert again.status_code == 404  # revoked 不可见（新 key 故走 404 门而非重放）
    async with pg.engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT status, is_default FROM user_providers WHERE id=:i"), {"i": pid}
            )
        ).one()
        task = (
            await conn.execute(
                text("SELECT status FROM tasks WHERE provider_id=:p"), {"p": str(pid)}
            )
        ).scalar_one()
    assert row.status == "revoked" and row.is_default is False and task == "failed"


async def test_delete_replay_after_revocation(provider_env, pg):
    """幂等重放优先于 404 门：同 key 的 DELETE 重放原 200（§7）。"""
    uid = await seed_active_user(pg, "delrp@example.com")
    pid = await seed_provider(pg, uid)
    async with auth_client() as client:
        await login(client, "delrp@example.com", "User-Passw0rd!")
        first = await client.delete(
            f"/api/v2/providers/{pid}", headers={"Idempotency-Key": "idem-dr-1"}
        )
        replay = await client.delete(
            f"/api/v2/providers/{pid}", headers={"Idempotency-Key": "idem-dr-1"}
        )
    assert first.status_code == 200 and replay.status_code == 200
    assert replay.json() == first.json()


async def test_put_delete_require_idempotency_key(provider_env, pg):
    uid = await seed_active_user(pg, "nokey2@example.com")
    pid = await seed_provider(pg, uid)
    async with auth_client() as client:
        await login(client, "nokey2@example.com", "User-Passw0rd!")
        assert (await client.put(f"/api/v2/providers/{pid}", json={})).status_code == 400
        assert (await client.delete(f"/api/v2/providers/{pid}")).status_code == 400


async def test_put_bad_uuid_400(provider_env, pg):
    """路径非 UUID → 400 VALIDATION_ERROR（get_provider_row 统一防护，防 500 兜底）。"""
    await seed_active_user(pg, "badpid@example.com")
    async with auth_client() as client:
        await login(client, "badpid@example.com", "User-Passw0rd!")
        resp = await client.put(
            "/api/v2/providers/not-a-uuid", json={}, headers={"Idempotency-Key": "idem-bu-1"}
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_write_without_csrf_403(provider_env, pg):
    """CSRF 门负例：登录后剔除 X-CSRF-Token → 403 CSRF_INVALID（防门序回归）。"""
    uid = await seed_active_user(pg, "nocsrf@example.com")
    async with auth_client() as client:
        await login(client, "nocsrf@example.com", "User-Passw0rd!")
        client.headers.pop("X-CSRF-Token", None)
        resp = await client.post(
            _CREATE,
            json={
                "catalog_id": "0197aaaa-7aaa-7aaa-7aaa-aaaaaaaaaaaa",
                "model_id": "gpt-4o-mini",
                "api_key": "sk-test-abcdef123456",
            },
            headers={"Idempotency-Key": "idem-nocsrf-1"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_INVALID"
    del uid
