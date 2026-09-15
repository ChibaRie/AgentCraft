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

import httpx
import pyotp
import pytest
from sqlalchemy import text

from backend.main import app
from backend.utils.crypto import encrypt_text, make_keyring
from backend.v2 import login_service
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
