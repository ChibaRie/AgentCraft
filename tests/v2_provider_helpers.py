"""Provider 域测试共享助手（superuser 种子 + 真实登录流）。

纪律：users/user_providers/tasks 属 owner-RLS 表，种子一律走 pg.engine（superuser，
admin/app role 无 INSERT policy）；认证态经真实登录端点建立（种 Argon2 哈希 →
POST /auth/login → 会话双 cookie）；每测试独立克隆库故限流窗口互不影响。
key_last4 一律服务层 plaintext[-4:]（裁决 D7：禁用 mask_key_hint 的 8 字符掩码）。
"""

import base64
import uuid as _uuid

import httpx
import pytest
from sqlalchemy import text

from backend.main import app
from backend.v2.runtime import get_v2_runtime
from backend.v2.security import hash_password
from tests.test_v2_runtime import make_v2_runtime

_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()
_PROVIDER_KEK_MATERIAL = base64.urlsafe_b64encode(bytes(range(32, 64))).decode()
_UA = "AgentCraft-ProviderTest/1.0"


@pytest.fixture
async def provider_env(pg, monkeypatch):
    """app/admin 双 role runtime + FastAPI 依赖 override + 密钥注入；yield runtime。

    两把密钥材料必须互异（对齐 D9「四把互不相同」纪律，勿同值双用）；
    SESSION_COOKIE_SECURE=false：默认 true 时种 Secure cookie，http.cookiejar
    对 http://testserver 拒绝回发 → 全部认证测试 401（Phase 2 house pattern：
    test_v2_auth_api.py:90-93 注释「SESSION_COOKIE_SECURE 下 jar 不持久化」）。
    conftest 已 setdefault ALLOW_INSECURE_SECRETS=true，config.py 校验门放行；
    _cookie_common 每请求现读 get_settings（无缓存），monkeypatch 即时生效。
    """
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("PROVIDER_KEY_ENCRYPTION_KEY", _PROVIDER_KEK_MATERIAL)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield rt
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.close()


async def seed_active_user(pg, email: str, password: str = "User-Passw0rd!") -> str:
    """superuser 播种 active 用户（Argon2 真实哈希），返回 user id。"""
    async with pg.engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), :e, :h, 'user', 'active') RETURNING id"
                ),
                {"e": email, "h": hash_password(password)},
            )
        ).scalar_one()


async def catalog_id_by_host(pg, allowed_host: str) -> str:
    """取 0002 种子目录行 id（种子 id 是 gen_random_uuid，不可硬编码）。"""
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT id FROM provider_catalog WHERE allowed_host = :h"),
                {"h": allowed_host},
            )
        ).scalar_one()


async def seed_provider(
    pg,
    user_id: str,
    *,
    catalog_host: str = "api.openai.com",
    model_id: str = "gpt-4o-mini",
    is_default: bool = False,
    status: str = "active",
    key_version: int = 1,
) -> str:
    """superuser 播种 user_providers 行（占位密文——CRUD 测试不解密），返回 id。"""
    cid = await catalog_id_by_host(pg, catalog_host)
    async with pg.engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO user_providers (id, user_id, catalog_id, model_id, "
                    "key_ciphertext, dek_wrapped, key_last4, key_version, status, is_default) "
                    "VALUES (gen_random_uuid(), :u, :c, :m, 'ct', 'dw', 'ST4K', :v, :s, :d) "
                    "RETURNING id"
                ),
                {
                    "u": user_id,
                    "c": cid,
                    "m": model_id,
                    "v": key_version,
                    "s": status,
                    "d": is_default,
                },
            )
        ).scalar_one()


async def seed_task_for_provider(
    pg, user_id: str, provider_id: str, *, status: str = "queued"
) -> str:
    """superuser 造 user 的 expert→revision→task 全链（tasks.status 参数化）。

    蓝本 tests/test_v2_rls.py::_seed_user_with_task：superuser 只绕 RLS 不绕
    FK/NOT NULL；content_json='{}'、content_sha256 为 64 hex；tasks 显式赋
    provider_key_version=1、event_sequence=0。

    账目/轮全链（审查修复：T8 对称释放断言需要真实持有物）：
    - uploading：+ held active reservation；
    - queued/ready：+ held active + task_root 两条 reservation + 占位 task_message
      + task_rounds(pending)（source_message_id NOT NULL FK → 必须先造消息）；
    - running/completed/aborted：不造 reservation/round（联动跳过场景）；
    - 未开始三态一律 upsert user_quota_usage.active_tasks += 1。
    返回 task id。
    """
    async with pg.engine.begin() as conn:
        expert_id = (
            await conn.execute(
                text(
                    "INSERT INTO experts (id, owner_id, status) "
                    "VALUES (gen_random_uuid(), :u, 'published') RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
        revision_id = (
            await conn.execute(
                text(
                    "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) VALUES (gen_random_uuid(), :x, "
                    ":u, 1, '{}', :h, 'published') RETURNING id"
                ),
                {"x": expert_id, "u": user_id, "h": "a" * 64},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE experts SET published_revision_id = :r WHERE id = :x"),
            {"r": revision_id, "x": expert_id},
        )
        catalog_id = (
            await conn.execute(
                text(
                    "SELECT catalog_id FROM user_providers WHERE id = :p"
                ),  # 列名是 catalog_id（非 provider_catalog_id）
                {"p": provider_id},
            )
        ).scalar_one()
        task_id = (
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, owner_id, expert_revision_id, provider_id, "
                    "provider_catalog_id, provider_model_id, provider_key_version, "
                    "event_sequence, status) VALUES (gen_random_uuid(), :u, :r, :p, :c, "
                    "'m', 1, 0, :s) RETURNING id"
                ),
                {"u": user_id, "r": revision_id, "p": provider_id, "c": catalog_id, "s": status},
            )
        ).scalar_one()
        uid = user_id if isinstance(user_id, _uuid.UUID) else _uuid.UUID(user_id)
        if status in ("uploading", "queued", "ready"):
            kinds = ["active"] + (["task_root"] if status in ("queued", "ready") else [])
            for kind in kinds:
                await conn.execute(
                    text(
                        "INSERT INTO task_reservations (id, task_id, user_id, kind, bytes, state) "
                        "VALUES (gen_random_uuid(), :t, :u, :k, 0, 'held')"
                    ),
                    {"t": task_id, "u": uid, "k": kind},
                )
            await conn.execute(
                text(
                    "INSERT INTO user_quota_usage (user_id, active_tasks, running_tasks, "
                    "retained_storage_bytes) VALUES (:u, 1, 0, 0) "
                    "ON CONFLICT (user_id) DO UPDATE SET "
                    "active_tasks = user_quota_usage.active_tasks + 1"
                ),
                {"u": uid},
            )
        if status in ("queued", "ready"):
            message_id = (
                await conn.execute(
                    text(
                        "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                        "author, content) VALUES (gen_random_uuid(), :t, :u, 1, 'user', 'seed') "
                        "RETURNING id"
                    ),
                    {"t": task_id, "u": uid},
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "INSERT INTO task_rounds (id, task_id, owner_id, source_message_id, "
                    "state, attempt) VALUES (gen_random_uuid(), :t, :u, :m, 'pending', 0)"
                ),
                {"t": task_id, "u": uid, "m": message_id},
            )
        return task_id


def auth_client(ip: str = "10.2.0.1") -> httpx.AsyncClient:
    """ASGITransport 驱动真实 app；client=(ip, port) 即 scope socket peer。"""
    transport = httpx.ASGITransport(app=app, client=(ip, 51000))
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers={"User-Agent": _UA}
    )


async def login(client: httpx.AsyncClient, email: str, password: str) -> str:
    """真实登录 → 双 cookie 会话 + 返回 CSRF token（调用方写请求自动带头）。

    csrf_token 嵌在 data 信封下（backend/v2/login_service.py:_login_body 的
    {"data": {"user", "csrf_token"}} 契约，A14 交付信道；auth.py:69 同源取值），
    与 tests/test_v2_login_mfa.py:188 等既有用例读法一致。
    """
    resp = await client.post("/api/v2/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    csrf = resp.json()["data"]["csrf_token"]
    client.headers["X-CSRF-Token"] = csrf
    return csrf
