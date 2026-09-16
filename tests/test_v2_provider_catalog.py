"""Provider 读面测试：目录只读面（enabled 过滤 + 字段清单防多泄）+ 用户列表。"""

from sqlalchemy import text

from tests.v2_provider_helpers import (
    auth_client,
    login,
    seed_active_user,
    seed_provider,
)


async def test_catalog_lists_enabled_only(provider_env, pg):
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE provider_catalog SET enabled=false WHERE allowed_host='api.openai.com'")
        )
    async with auth_client() as client:
        await seed_active_user(pg, "cat@example.com")
        await login(client, "cat@example.com", "User-Passw0rd!")
        resp = await client.get("/api/providers/catalog")
    assert resp.status_code == 200
    items = resp.json()["data"]
    hosts = {i["allowed_host"] for i in items}
    assert "api.openai.com" not in hosts and "api.deepseek.com" in hosts
    for item in items:
        assert set(item.keys()) == {"id", "display_name", "allowed_host", "models"}  # D13 防多泄


async def test_catalog_requires_auth(provider_env):
    """无 cookie → 401 SESSION_EXPIRED。

    必须经 provider_env override get_v2_runtime：get_v2_auth 自身依赖
    get_v2_runtime（session_service.py），未配置时 503 先于 cookie 检查抛出——
    无 override 时 CI（无 .env）得 503 而非 401，本机有 .env V2 DSN 时会物化
    真实引擎并污染 runtime 模块级单例（Phase 2 惯例：401 断言也在 override 下）。
    """
    async with auth_client() as client:
        resp = await client.get("/api/providers/catalog")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_EXPIRED"


async def test_provider_list_active_only_and_exact_fields(provider_env, pg):
    uid = await seed_active_user(pg, "list@example.com")
    keep = await seed_provider(pg, uid, is_default=True)
    await seed_provider(pg, uid, model_id="gpt-4o", status="revoked")
    other = await seed_active_user(pg, "other@example.com")
    await seed_provider(pg, other)  # RLS 隔离：他人行不可见
    async with auth_client() as client:
        await login(client, "list@example.com", "User-Passw0rd!")
        resp = await client.get("/api/providers")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [p["id"] for p in data] == [str(keep)]
    row = data[0]
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
    assert row["model_id"] == "gpt-4o-mini" and row["is_default"] is True
    assert row["key_last4"] == "ST4K" and row["key_version"] == 1
