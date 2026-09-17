"""admin 面测试共享助手（Phase 7 T1）。**自包含**：不 import、不改动
tests/test_v2_login_mfa.py——_mfa_envelope 形态按 login_mfa.py:90-97 复制
（同材料 keyring + login_service.mfa_secret_aad），生产代码正常导入。

- ``admin_env`` fixture：三件套注入 RATE_LIMIT_HMAC_KEY + MFA_ENCRYPTION_KEY +
  SESSION_COOKIE_SECURE=false（同 provider_env 形态；两把密钥同材料对齐
  test_v2_login_mfa.flow_env——信封加密材料必须与注入 Settings 的一致）；
- ``seed_admin_user(pg, email, *, password="pw-123456", configure_mfa=True) -> str``：
  superuser 播种 role='admin'/status='active' 行；configure_mfa=True 自动生成
  totp_secret 并种 mfa_secret_enc 信封；False=不配置（D2② 负例形态）；
- ``admin_client(pg, rt, *, email) -> (client, csrf, admin_id)``：内部恒
  seed_admin_user(configure_mfa=True) + create_session(mfa_verified=True)
  （owner_session 单事务，命中 sessions_app_insert WITH CHECK）+ ac_session
  cookie + X-CSRF-Token 头；绑定全局 app（Phase 7 admin 路由 T2+ 挂载后直驱）。
"""

import base64
import json
import uuid as _uuid
from datetime import datetime, timezone

import httpx
import pyotp
import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.main import app
from backend.utils.crypto import encrypt_text, make_keyring
from backend.v2 import login_service
from backend.v2.author_service import create_entity
from backend.v2.ids import uuid7
from backend.v2.runtime import get_v2_runtime, owner_session
from backend.v2.security import hash_password
from backend.v2.session_service import COOKIE_NAME, create_session
from tests.test_v2_runtime import make_v2_runtime

_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()
_UA = "AgentCraft-AdminTest/1.0"
_IP = "10.7.0.1"


@pytest.fixture
async def admin_env(pg, monkeypatch):
    """三件套密钥注入 + app/admin 双 role runtime + FastAPI 依赖 override；yield runtime。

    SESSION_COOKIE_SECURE=false：默认 true 时会话 cookie 带 Secure，
    http.cookiejar 对 http://testserver 拒绝回发 → 认证测试全 401（Phase 2 house
    pattern）。conftest 已 setdefault ALLOW_INSECURE_SECRETS=true，校验门放行。
    """
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("MFA_ENCRYPTION_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield rt
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.close()  # 同步 dispose 双引擎连接池（sync_engine.dispose）


def _mfa_envelope(secret: str, user_id: str) -> str:
    """测试 keyring 直接构造 TOTP 信封（形态复制自 test_v2_login_mfa.py:90-97，
    不导入；材料与 admin_env 注入 Settings 的 MFA_ENCRYPTION_KEY 同源）。"""
    _, keyring = make_keyring(f"primary:{_KEY_MATERIAL}")
    envelope = encrypt_text(
        secret, aad=login_service.mfa_secret_aad(user_id), keyring=keyring, active_kid="primary"
    )
    return json.dumps(envelope)


async def seed_admin_user(
    pg,
    email: str,
    *,
    password: str = "pw-123456",
    configure_mfa: bool = True,
) -> str:
    """superuser 播种 admin 用户（users 属 owner-RLS 且 admin 无 INSERT policy），
    返回 user id 字符串。

    configure_mfa=True：生成 totp_secret 并以测试 keyring 种 mfa_secret_enc 信封
    （密文可用 decrypt_text + mfa_secret_aad 解回）；False=不配置（mfa_secret_enc
    NULL——D2②「未配置者永不过门」的负例形态）。email 小写归一（login 按小写查）。"""
    admin_id = _uuid.uuid4()
    secret = pyotp.random_base32() if configure_mfa else None
    enc = _mfa_envelope(secret, str(admin_id)) if secret is not None else None
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc) "
                "VALUES (:i, :e, :p, 'admin', 'active', :m)"
            ),
            {
                "i": str(admin_id),
                "e": email.lower(),
                "p": hash_password(password),
                "m": enc,
            },
        )
    return str(admin_id)


async def admin_client(pg, rt, *, email: str) -> tuple[httpx.AsyncClient, str, str]:
    """恒 seed_admin_user(configure_mfa=True) + create_session(mfa_verified=True)
    + ac_session cookie（HttpOnly 会话 cookie 以明文直投 cookie jar）；X-CSRF-Token
    头预置（写方法免逐请求带头）。返回 (client, csrf, admin_id)；调用方 aclose()。"""
    admin_id = await seed_admin_user(pg, email)
    async with owner_session(rt, admin_id) as db:
        session_token, csrf = await create_session(
            db, user_id=_uuid.UUID(admin_id), device_label=_UA, mfa_verified=True
        )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(_IP, 51000)),
        base_url="http://testserver",
        headers={"User-Agent": _UA, "X-CSRF-Token": csrf},
        cookies={COOKIE_NAME: session_token},
    )
    return client, csrf, admin_id


# ---------------------------------------------------------------------------
# 用户管理面共享助手（Phase 9 T6 拆分自 test_v2_admin_users.py 顶部）
# ---------------------------------------------------------------------------

_USERS = "/api/admin/users"
_USERS_UA = "AgentCraft-AdminUsersTest/1.0"
_CANARY_CIPHERTEXT = "CANARY-CIPHERTEXT-never-leak-0123456789"


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


async def _seed_user(
    pg,
    email: str,
    *,
    status: str = "active",
    role: str = "user",
    created_at: datetime | None = None,
) -> str:
    """播种普通用户行（app role 无 users INSERT），返回 user id 字符串。"""
    uid = str(_uuid.uuid4())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, "
                "mfa_secret_enc, created_at) VALUES (:i, :e, 'h', :r, :s, NULL, :ca)"
            ),
            {
                "i": uid,
                "e": email,
                "r": role,
                "s": status,
                "ca": created_at or datetime.now(timezone.utc),
            },
        )
    return uid


async def _seed_task_chain(pg, owner_id: str) -> dict:
    """播种最小任务前置链（experts/expert_revisions/user_providers），返回外键 id 集合。

    tasks 三外键（expert_revision_id RESTRICT / provider_id RESTRICT）必须先行落位；
    user_providers 密文用金丝雀字面（detail 无 Key 红线断言的泄漏探针）。
    """
    async with pg.engine.begin() as conn:
        catalog_id = (
            await conn.execute(text("SELECT id FROM provider_catalog LIMIT 1"))
        ).scalar_one()
        expert_id, rev_id, provider_id = str(uuid7()), str(uuid7()), str(uuid7())
        await conn.execute(
            text("INSERT INTO experts (id, owner_id, status) VALUES (:i, :o, 'draft')"),
            {"i": expert_id, "o": owner_id},
        )
        await conn.execute(
            text(
                "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                "content_json, content_sha256, status) "
                "VALUES (:i, :e, :o, 1, CAST(:cj AS jsonb), :sha, 'draft')"
            ),
            {
                "i": rev_id,
                "e": expert_id,
                "o": owner_id,
                "cj": json.dumps({"name": "seed-expert"}),
                "sha": "a" * 64,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO user_providers (id, user_id, catalog_id, base_url, model_id, "
                "key_ciphertext, dek_wrapped, key_last4, key_version, status, is_default) "
                "VALUES (:i, :o, :c, 'https://admin-seed.example.com/v1', 'gpt-4o-mini', "
                ":kc, :dw, 'ab12', 1, 'active', false)"
            ),
            {
                "i": provider_id,
                "o": owner_id,
                "c": catalog_id,
                "kc": _CANARY_CIPHERTEXT,
                "dw": "dw",
            },
        )
        return {
            "expert_id": expert_id,
            "revision_id": rev_id,
            "provider_id": provider_id,
            "catalog_id": str(catalog_id),
        }


async def _seed_task(pg, owner_id: str, chain: dict, *, status: str = "completed") -> str:
    tid = str(uuid7())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, expert_revision_id, provider_id, "
                "provider_catalog_id, provider_model_id, provider_key_version, status, "
                "event_sequence) VALUES (:i, :o, :r, :p, :c, 'gpt-4o-mini', 1, :s, 0)"
            ),
            {
                "i": tid,
                "o": owner_id,
                "r": chain["revision_id"],
                "p": chain["provider_id"],
                "c": chain["catalog_id"],
                "s": status,
            },
        )
    return tid


async def _seed_quota(
    pg, user_id: str, daily: int, active: int, running: int, retained: int
) -> None:
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_quotas (user_id, max_daily_tasks, max_active_tasks, "
                "max_running_tasks, max_retained_storage_bytes) "
                "VALUES (CAST(:u AS uuid), :d, :a, :r, :s)"
            ),
            {"u": user_id, "d": daily, "a": active, "r": running, "s": retained},
        )


async def _seed_usage(pg, user_id: str, active: int, running: int, retained: int) -> None:
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_quota_usage (user_id, active_tasks, running_tasks, "
                "retained_storage_bytes) VALUES (CAST(:u AS uuid), :a, :r, :s)"
            ),
            {"u": user_id, "a": active, "r": running, "s": retained},
        )


async def _one(pg, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


async def _count(pg, table: str, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {})
        ).scalar_one()


async def _user_client(pg, rt, *, email: str) -> httpx.AsyncClient:
    """role='user'（无 TOTP）+ mfa_verified 会话客户端——403 FORBIDDEN 即门序①
    role 门先行的证据（同 test_v2_admin_invitations.py 形态）。"""
    uid = str(_uuid.uuid4())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc) "
                "VALUES (:i, :e, 'h', 'user', 'active', NULL)"
            ),
            {"i": uid, "e": email},
        )
    async with owner_session(rt, uid) as db:
        token, csrf = await create_session(
            db, user_id=_uuid.UUID(uid), device_label=_USERS_UA, mfa_verified=True
        )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.7.0.9", 51002)),
        base_url="http://testserver",
        headers={"User-Agent": _USERS_UA, "X-CSRF-Token": csrf},
        cookies={COOKIE_NAME: token},
    )


async def _rewind_mfa_verified(pg, user_id: str, *, hours: int) -> None:
    """superuser 回拨会话 mfa_verified_at（sessions owner-RLS，superuser 绕过）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET mfa_verified_at = now() - make_interval(secs => :s) "
                "WHERE user_id = CAST(:u AS uuid)"
            ),
            {"s": hours * 3600, "u": user_id},
        )


async def _authoring_gate_status(rt, uid: str, key: str) -> int:
    """直调 authoring 服务探测 expert_author 消费门：403（门闭）/200（门开）。"""
    try:
        await create_entity(
            rt,
            user_id=uid,
            target="experts",
            content={"name": f"gate-probe-{key}"},
            idem_key=f"gate-{key}",
            idem_hash="h-" + key,
        )
    except HTTPException as exc:
        return exc.status_code
    return 200


# ---------- 请求助手 ----------


async def _put_quotas(client, uid: str, *, body: dict | None = None, key: str = "k-q1"):
    payload = body if body is not None else {"max_daily_tasks": 10, "reason": "容量评估"}
    return await client.put(
        f"{_USERS}/{uid}/quotas", json=payload, headers={"Idempotency-Key": key}
    )


async def _grant(client, uid: str, *, kind: str = "expert_author", key: str = "k-g1"):
    return await client.post(
        f"{_USERS}/{uid}/entitlements",
        json={"kind": kind, "reason": "作者资格审核通过"},
        headers={"Idempotency-Key": key},
    )


async def _revoke(
    client,
    uid: str,
    *,
    kind: str = "expert_author",
    reason: str = "作者资格撤销",
    key: str = "k-r1",
):
    return await client.request(
        "DELETE",
        f"{_USERS}/{uid}/entitlements",
        json={"kind": kind, "reason": reason},
        headers={"Idempotency-Key": key},
    )
