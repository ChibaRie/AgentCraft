"""连通性测试端点：SSRF 硬约束（D10）+ 限流 + 解密消费 + 红线。

上游一律 httpx.MockTransport 脚本化，真实网络零依赖。HTTP 行为经服务函数
transport 注入直测；端点层仅测门序（限流 429 / 404 / CATALOG_ITEM_DISABLED）。
测试行用 T2 KeySealer 造真实信封（open 可解），非占位密文。
"""

import logging

import httpx
from sqlalchemy import text

from backend.v2 import provider_crypto, provider_service
from backend.v2.ids import uuid7
from tests.v2_provider_helpers import (
    auth_client,
    catalog_id_by_host,
    login,
    seed_active_user,
)


async def _seed_provider_with_real_key(pg, uid: str) -> str:
    """superuser 播种真实信封行（T2 KeySealer 造密文），返回 provider id。"""
    cid = await catalog_id_by_host(pg, "api.openai.com")
    pid = uuid7()
    ct, dw = provider_crypto.key_sealer().seal("sk-live-abcdef123456", provider_id=str(pid))
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_providers (id, user_id, catalog_id, model_id, key_ciphertext, "
                "dek_wrapped, key_last4, key_version, status, is_default) "
                "VALUES (:i, :u, :c, 'gpt-4o-mini', :ct, :dw, '3456', 1, 'active', false)"
            ),
            {"i": str(pid), "u": uid, "c": cid, "ct": ct, "dw": dw},
        )
    return str(pid)


async def _call_service(rt, uid, pid, handler):
    return await provider_service.test_provider_connectivity(
        rt, user_id=str(uid), provider_id=str(pid), transport=httpx.MockTransport(handler)
    )


async def test_ok_counts_models(provider_env, pg):
    uid = await seed_active_user(pg, "t@example.com")
    pid = await _seed_provider_with_real_key(pg, uid)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        assert request.url.host == "api.openai.com"
        assert request.url.path == "/v1/models"  # 不叠 path_prefix（D10）
        assert request.headers["authorization"] == "Bearer sk-live-abcdef123456"
        assert request.headers["accept"] == "application/json"
        return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]})

    result = await _call_service(provider_env, uid, pid, handler)
    assert set(result.keys()) == {"ok", "latency_ms", "models_visible"}  # 契约恰三字段（D10）
    assert result["ok"] is True and result["models_visible"] == 2
    assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0


async def test_non_2xx_ok_false(provider_env, pg):
    uid = await seed_active_user(pg, "t2a@example.com")
    pid = await _seed_provider_with_real_key(pg, uid)
    result = await _call_service(
        provider_env, uid, pid, lambda req: httpx.Response(502, text="bad gateway")
    )
    assert result["ok"] is False and result["models_visible"] == 0


async def test_2xx_non_json_counts_zero(provider_env, pg):
    """ok 按 2xx 判；非 JSON → 计数 0（不外泄内容）。"""
    uid = await seed_active_user(pg, "t2b@example.com")
    pid = await _seed_provider_with_real_key(pg, uid)
    result = await _call_service(
        provider_env, uid, pid, lambda req: httpx.Response(200, text="<html>not json</html>")
    )
    assert result["ok"] is True and result["models_visible"] == 0


async def test_redirect_not_followed(provider_env, pg):
    uid = await seed_active_user(pg, "t3@example.com")
    pid = await _seed_provider_with_real_key(pg, uid)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://evil.example/v1/models"})
        return httpx.Response(200, json={"data": []})

    result = await _call_service(provider_env, uid, pid, handler)
    assert len(calls) == 1 and result["ok"] is False  # 不跟随（显式 follow_redirects=False）


async def test_response_body_over_2mb_fails(provider_env, pg):
    uid = await seed_active_user(pg, "t4@example.com")
    pid = await _seed_provider_with_real_key(pg, uid)
    big = b"x" * (3 * 1024 * 1024)
    result = await _call_service(
        provider_env, uid, pid, lambda req: httpx.Response(200, content=big)
    )
    assert result["ok"] is False and result["models_visible"] == 0


async def test_connect_error_ok_false(provider_env, pg):
    """连接失败 → 统一失败形态（不抛异常、不泄上游细节）。"""
    uid = await seed_active_user(pg, "t4b@example.com")
    pid = await _seed_provider_with_real_key(pg, uid)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await _call_service(provider_env, uid, pid, handler)
    assert result["ok"] is False and result["models_visible"] == 0


async def test_upstream_error_never_leaks(caplog, provider_env, pg):
    """红线：上游响应体与 Key 明文不回传不落日志（Eng §6）。"""
    uid = await seed_active_user(pg, "t5@example.com")
    pid = await _seed_provider_with_real_key(pg, uid)
    upstream_secret = "UPSTREAM-SECRET-CONTENT"
    with caplog.at_level(logging.DEBUG):
        result = await _call_service(
            provider_env, uid, pid, lambda req: httpx.Response(500, text=upstream_secret)
        )
    assert result["ok"] is False
    assert upstream_secret not in caplog.text
    assert "sk-live" not in caplog.text


async def test_endpoint_rate_limited_10_per_hour(provider_env, pg):
    """限流 10/h/用户：第 11 次 429 + Retry-After（enforce 先于业务）。"""
    from backend.v2.rate_limit import enforce, hmac_subject

    uid = await seed_active_user(pg, "t6@example.com")
    for _ in range(10):
        async with provider_env.app_factory() as db:
            await enforce(db, scope="provider_test", subjects=[hmac_subject("user", str(uid))])
    pid = await _seed_provider_with_real_key(pg, uid)
    async with auth_client() as client:
        await login(client, "t6@example.com", "User-Passw0rd!")
        resp = await client.post(f"/api/providers/{pid}/test")
    assert resp.status_code == 429
    assert "retry-after" in {k.lower() for k in resp.headers}


async def test_endpoint_disabled_catalog_400(provider_env, pg):
    """D11：目录禁用 → /test 拒绝（400 CATALOG_ITEM_DISABLED）。"""
    uid = await seed_active_user(pg, "t7@example.com")
    pid = await _seed_provider_with_real_key(pg, uid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE provider_catalog SET enabled=false WHERE allowed_host='api.openai.com'")
        )
    async with auth_client() as client:
        await login(client, "t7@example.com", "User-Passw0rd!")
        resp = await client.post(f"/api/providers/{pid}/test")
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "CATALOG_ITEM_DISABLED"


async def test_endpoint_revoked_or_foreign_404(provider_env, pg):
    """端点层 404 门（revoked / 跨用户）。注意：本用例不断言 200 成功路径——
    端点层无 transport 注入缝，200 断言会对 api.openai.com 发起真实外呼
    （违背本文件「真实网络零依赖」）；成功路径已由 service 层 MockTransport
    用例覆盖。"""
    uid_a = await seed_active_user(pg, "t8a@example.com")
    uid_b = await seed_active_user(pg, "t8b@example.com")
    pid_revoked = await _seed_provider_with_real_key(pg, uid_b)
    await _seed_provider_with_real_key(pg, uid_a)  # own 行存在但不打它
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE user_providers SET status='revoked' WHERE id=:i"), {"i": pid_revoked}
        )
    async with auth_client() as client:
        await login(client, "t8a@example.com", "User-Passw0rd!")
        cross = await client.post(f"/api/providers/{pid_revoked}/test")
    assert cross.status_code == 404


async def test_endpoint_tampered_dek_400_key_version_revoked(caplog, provider_env, pg):
    """EncryptionError → 400 KEY_VERSION_REVOKED（干净文案，from None 隐藏异常链）；
    失败路径日志零 Key 材料（service+端点层红线，补 T2 仅原语层的盲区）。"""
    uid = await seed_active_user(pg, "t9@example.com")
    pid = await _seed_provider_with_real_key(pg, uid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE user_providers SET dek_wrapped = 'tampered' WHERE id = :i"), {"i": pid}
        )
    with caplog.at_level(logging.DEBUG):
        resp = await client_post_test("t9@example.com", pid)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "KEY_VERSION_REVOKED"
    assert "sk-live" not in caplog.text


async def test_endpoint_bad_uuid_400(provider_env, pg):
    """路径非 UUID → 400 VALIDATION_ERROR（get_provider_row 统一防护）。"""
    await seed_active_user(pg, "t10@example.com")
    resp = await client_post_test("t10@example.com", "not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def client_post_test(email: str, pid: str):
    """登录 + POST /{id}/test（独立 helper：坏 UUID 用例与限流计数隔离）。"""
    async with auth_client() as client:
        await login(client, email, "User-Passw0rd!")
        return await client.post(f"/api/providers/{pid}/test")
