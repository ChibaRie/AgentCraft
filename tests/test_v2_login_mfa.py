"""登录 + MFA 挑战 + TOTP 注册端到端测试（Task 11）。

契约出处：task-11-brief + Supplement §2 + 裁决 A9/A11/A13/A14/A15。

- login（公开端点，无 CSRF 无幂等键）：限流 login（10/900s，主体
  [HMAC(email), HMAC(ip)]，先于一切查询）→ admin 按 email 查用户 → 未知/已删除
  → equalize_login_timing + 统一 401（防账号枚举，A11）；验密失败同文案 → 状态门
  （suspended/deleting 403，pending 放行）→ TOTP 已启用 → 200 挑战不建会话 →
  否则 owner 事务建会话 + 双 cookie（A14）。
- login/mfa（公开端点）：挑战未知/过期/码错统一 401 MFA_INVALID（挑战存储自管
  尝试计数，5 次耗尽销毁）；失败路径先 enforce mfa_failure（429 优先于 401）；
  成功建会话 mfa_verified_at 非空。
- setup/activate/disable（认证端点，get_v2_auth 门序）：setup 产 secret + otpauth
  （内存 pending 10 分钟，重复覆盖）；activate 信封加密落 users.mfa_secret_enc +
  当前会话盖 mfa_verified_at；admin 不可停用（A9：403 FORBIDDEN）。
- HTTP 层沿用 T10 house pattern：httpx.AsyncClient(ASGITransport(app, client=(ip,
  port))) 于测试自身循环驱动真实 app；authenticated_client 复用自
  tests/test_v2_verification_flow.py。
- 密钥注入：RATE_LIMIT_HMAC_KEY / MFA_ENCRYPTION_KEY 每次现读 Settings，经
  monkeypatch.setenv 注入一次性 b64url 材料（T9/T10 flow_env 同款）；TOTP 种子
  用户由测试以同材料 keyring 直接构造信封写 mfa_secret_enc。
- A13 内存 store（challenge_store / pending_mfa_secrets）为模块级单例：autouse
  夹具用例前后 reset；过期模拟按裁决直接操纵实例（回拨 created_at）。
"""

import base64
import json
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

import httpx
import pyotp
import pytest
from sqlalchemy import text

from backend.main import app
from backend.utils.crypto import decrypt_text, encrypt_text, make_keyring
from backend.v2 import login_service
from backend.v2.rate_limit import hmac_subject
from backend.v2.runtime import get_v2_runtime, owner_session
from backend.v2.security import generate_token, hash_password
from backend.v2.session_service import COOKIE_NAME, CSRF_COOKIE_NAME, create_session
from tests.conftest import PgDb
from tests.test_v2_runtime import make_v2_runtime
from tests.test_v2_verification_flow import authenticated_client, http_client

_ROUTE_LOGIN = "/api/v2/auth/login"
_ROUTE_LOGIN_MFA = "/api/v2/auth/login/mfa"
_ROUTE_MFA_SETUP = "/api/v2/auth/mfa/setup"
_ROUTE_MFA_ACTIVATE = "/api/v2/auth/mfa/activate"
_ROUTE_MFA_DISABLE = "/api/v2/auth/mfa"
_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()  # 仅测试材料

# 统一失败文案（契约钉死；响应体形状见 backend/main.py 错误处理器）
_UNIFORM_401 = {"error": {"code": "INVALID_CREDENTIALS", "message": "账号或密码不正确"}}
_MFA_INVALID_401 = {"error": {"code": "MFA_INVALID", "message": "验证码无效或已过期"}}
_MFA_INVALID_400 = _MFA_INVALID_401  # activate 失败同码同文案，仅状态 400
_SUSPENDED_403 = {"error": {"code": "ACCOUNT_SUSPENDED", "message": "账户已被停用"}}
_DELETING_403 = {"error": {"code": "ACCOUNT_DELETING", "message": "账户注销处理中"}}
_FORBIDDEN_403 = {"error": {"code": "FORBIDDEN", "message": "管理员不可停用 TOTP"}}


@pytest.fixture
async def flow_env(pg: PgDb, monkeypatch):
    """app/admin 双 role runtime + FastAPI 依赖 override + 双密钥注入；yield runtime。"""
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("MFA_ENCRYPTION_KEY", _KEY_MATERIAL)
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield rt
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.close()  # 同步 dispose 双引擎连接池（sync_engine.dispose）


@pytest.fixture(autouse=True)
def _reset_mfa_stores():
    """A13：内存 store 模块级单例，进程内跨用例残留——用例前后清空。"""
    login_service.challenge_store.reset()
    login_service.pending_mfa_secrets.reset()
    yield
    login_service.challenge_store.reset()
    login_service.pending_mfa_secrets.reset()


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


def _mfa_envelope(secret: str, user_id: str) -> str:
    """测试 keyring 直接构造信封（材料与 flow_env 注入 Settings 的同源）。"""
    _, keyring = make_keyring(f"primary:{_KEY_MATERIAL}")
    envelope = encrypt_text(
        secret, aad=login_service.mfa_secret_aad(user_id), keyring=keyring, active_kid="primary"
    )
    return json.dumps(envelope)


async def _seed_user(
    pg: PgDb,
    email: str,
    *,
    password: str = "pw-123456",
    status: str = "active",
    role: str = "user",
    totp_secret: str | None = None,
) -> str:
    """播种用户行（可带 TOTP 信封），返回 user id 字符串。"""
    new_id = uuid.uuid4()
    enc = _mfa_envelope(totp_secret, str(new_id)) if totp_secret is not None else None
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc) "
                "VALUES (:i, :e, :p, :r, :s, :m)"
            ),
            {
                "i": str(new_id),
                "e": email,
                "p": hash_password(password),
                "r": role,
                "s": status,
                "m": enc,
            },
        )
    return str(new_id)


async def _count(pg: PgDb, table: str, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {})
        ).scalar_one()


async def _one(pg: PgDb, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


def _cookie_jar(resp: httpx.Response) -> SimpleCookie:
    """解析 set-cookie 响应头（属性断言用）。"""
    jar = SimpleCookie()
    for value in resp.headers.get_list("set-cookie"):
        jar.load(value)
    return jar


def _current_code(secret: str) -> str:
    """当前 30s 窗口的有效 TOTP 码（pyotp .at 免睡眠，brief 钉死）。"""
    return pyotp.TOTP(secret).at(datetime.now(timezone.utc))


def _wrong_code(secret: str) -> str:
    """与当前时间码必然不同的 6 位码（排除万年一遇的假阴性）。"""
    current = _current_code(secret)
    return "000000" if current != "000000" else "000001"


async def _login_challenge(email: str, password: str) -> str:
    """登录拿 MFA 挑战 id（挑战存内存 store，跨 HTTP 客户端可用）。"""
    async with http_client("10.0.30.99") as client:
        resp = await client.post(_ROUTE_LOGIN, json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["mfa_required"] is True
    return data["mfa_challenge_id"]


# ---------- login：正常路径（会话 + 双 cookie 属性断言）----------


async def test_login_success_creates_session_and_cookies(pg, flow_env):
    uid = await _seed_user(pg, "login-ok@example.com", password="s3cret-pw")
    async with http_client("10.0.30.1") as client:
        resp = await client.post(
            _ROUTE_LOGIN, json={"email": "login-ok@example.com", "password": "s3cret-pw"}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["user"] == {
        "id": uid,
        "email": "login-ok@example.com",
        "role": "user",
        "status": "active",
    }
    assert body["data"]["csrf_token"]  # csrf 明文入响应体（A14 交付信道）

    row = await _one(
        pg, "SELECT mfa_verified_at, device_label FROM sessions WHERE user_id = :u", {"u": uid}
    )
    assert row["mfa_verified_at"] is None  # 正常登录未过 MFA
    assert row["device_label"] == "AgentCraft-FlowTest/1.0"

    jar = _cookie_jar(resp)
    assert set(jar) == {COOKIE_NAME, CSRF_COOKIE_NAME}
    session_cookie, csrf_cookie = jar[COOKIE_NAME], jar[CSRF_COOKIE_NAME]
    assert session_cookie["httponly"] is True  # XSS 面收口
    assert session_cookie["samesite"] == "strict"
    assert session_cookie["secure"] is True
    assert not csrf_cookie["httponly"]  # A14：SPA 必须可读
    assert csrf_cookie["samesite"] == "strict"


async def test_admin_without_totp_gets_normal_session(pg, flow_env):
    """无 TOTP 的 admin 落常规会话（mfa_verified_at 空；Phase 7 门负责拦持有期）。"""
    uid = await _seed_user(pg, "admin-login@example.com", password="admin-pw", role="admin")
    async with http_client("10.0.30.2") as client:
        resp = await client.post(
            _ROUTE_LOGIN, json={"email": "admin-login@example.com", "password": "admin-pw"}
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["user"]["role"] == "admin"
    assert "mfa_required" not in resp.json()["data"]
    row = await _one(pg, "SELECT mfa_verified_at FROM sessions WHERE user_id = :u", {"u": uid})
    assert row["mfa_verified_at"] is None


# ---------- login：防枚举（统一 401 逐字节一致 + 时序均衡）----------


async def test_login_failure_modes_uniform_401_byte_identical(pg, flow_env):
    await _seed_user(pg, "wrongpw@example.com", password="right-pw")
    await _seed_user(pg, "deleted@example.com", password="del-pw", status="deleted")
    bodies = []
    async with http_client("10.0.30.3") as client:
        requests = [
            ("unknown_account", {"email": "ghost@example.com", "password": "x"}),
            ("wrong_password", {"email": "wrongpw@example.com", "password": "bad-pw"}),
            ("deleted_correct_pw", {"email": "deleted@example.com", "password": "del-pw"}),
            ("deleted_wrong_pw", {"email": "deleted@example.com", "password": "nope"}),
        ]
        for mode, payload in requests:
            resp = await client.post(_ROUTE_LOGIN, json=payload)
            assert resp.status_code == 401, mode
            assert resp.json() == _UNIFORM_401, mode
            bodies.append(resp.content)
    assert len(set(bodies)) == 1  # 全部失效形态响应体逐字节一致（防探测）
    assert await _count(pg, "sessions") == 0  # 失败路径零会话副作用


async def test_login_timing_unknown_vs_wrong_password_balanced(pg, flow_env):
    """A11：未知账号哑验证均衡——两路径耗时比 ≤ 2x（宽松断言，min-of-3 防抖）。"""
    await _seed_user(pg, "timing@example.com", password="timing-pw")
    wrong = {"email": "timing@example.com", "password": "not-the-pw"}
    unknown = {"email": "ghost-timing@example.com", "password": "whatever"}
    async with http_client("10.0.30.4") as client:
        # 预热：argon2 首调用含内存分配抖动，先各跑一次再计时
        await client.post(_ROUTE_LOGIN, json=wrong)
        await client.post(_ROUTE_LOGIN, json=unknown)
        wrong_times, unknown_times = [], []
        for _ in range(3):
            wrong_times.append(
                (await client.post(_ROUTE_LOGIN, json=wrong)).elapsed.total_seconds()
            )
            unknown_times.append(
                (await client.post(_ROUTE_LOGIN, json=unknown)).elapsed.total_seconds()
            )
    wrong_s, unknown_s = min(wrong_times), min(unknown_times)
    assert max(unknown_s, wrong_s) <= 2 * min(unknown_s, wrong_s)


# ---------- login：状态门与大小写规范化 ----------


async def test_login_status_gates_suspended_and_deleting(pg, flow_env):
    await _seed_user(pg, "susp@example.com", password="pw-1", status="suspended")
    await _seed_user(pg, "deleting@example.com", password="pw-2", status="deleting")
    async with http_client("10.0.30.5") as client:
        suspended = await client.post(
            _ROUTE_LOGIN, json={"email": "susp@example.com", "password": "pw-1"}
        )
        deleting = await client.post(
            _ROUTE_LOGIN, json={"email": "deleting@example.com", "password": "pw-2"}
        )

    assert suspended.status_code == 403
    assert suspended.json() == _SUSPENDED_403
    assert deleting.status_code == 403
    assert deleting.json() == _DELETING_403
    assert await _count(pg, "sessions") == 0


async def test_pending_user_can_login(pg, flow_env):
    uid = await _seed_user(pg, "pending@example.com", password="pending-pw", status="pending")
    async with http_client("10.0.30.6") as client:
        resp = await client.post(
            _ROUTE_LOGIN, json={"email": "pending@example.com", "password": "pending-pw"}
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["user"] == {
        "id": uid,
        "email": "pending@example.com",
        "role": "user",
        "status": "pending",
    }
    assert await _count(pg, "sessions", "user_id = :u", {"u": uid}) == 1


async def test_login_email_case_insensitive(pg, flow_env):
    """email 大小写变体落在同一账号与同一 HMAC 主体（规范化小写先行）。"""
    uid = await _seed_user(pg, "case@example.com", password="case-pw")
    async with http_client("10.0.30.7") as client:
        resp = await client.post(
            _ROUTE_LOGIN, json={"email": "CASE@EXAMPLE.COM", "password": "case-pw"}
        )

    assert resp.status_code == 200
    assert resp.json()["data"]["user"]["id"] == uid
    assert resp.json()["data"]["user"]["email"] == "case@example.com"
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'login' AND subject_hash = :h",
        {"h": hmac_subject("email", "case@example.com")},
    )
    assert n == 1  # 限流主体用规范化小写形态


# ---------- login：TOTP 用户 → 挑战（不建会话）----------


async def test_login_totp_user_returns_challenge_without_session(pg, flow_env):
    secret = pyotp.random_base32()
    uid = await _seed_user(pg, "totp@example.com", password="totp-pw", totp_secret=secret)
    async with http_client("10.0.30.8") as client:
        resp = await client.post(
            _ROUTE_LOGIN, json={"email": "totp@example.com", "password": "totp-pw"}
        )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["mfa_required"] is True
    assert isinstance(data["mfa_challenge_id"], str) and len(data["mfa_challenge_id"]) >= 40
    assert resp.headers.get_list("set-cookie") == []  # 挑战路径不种 cookie
    assert await _count(pg, "sessions") == 0  # 未过 MFA 不建会话
    entry = login_service.challenge_store._entries[data["mfa_challenge_id"]]
    assert entry.user_id == uid and entry.attempts == 0


# ---------- login/mfa：挑战未知/过期统一 401 ----------


async def test_mfa_challenge_unknown_and_expired_uniform_401(pg, flow_env):
    secret = pyotp.random_base32()
    await _seed_user(pg, "mfa-exp@example.com", password="pw-exp", totp_secret=secret)
    cid = await _login_challenge("mfa-exp@example.com", "pw-exp")
    # A13 授权直接操纵实例：回拨创建时间 6 分钟（TTL 5 分钟）模拟过期
    entry = login_service.challenge_store._entries[cid]
    login_service.challenge_store._entries[cid] = replace(
        entry, created_at=entry.created_at - timedelta(minutes=6)
    )
    async with http_client("10.0.31.1") as client:
        unknown = await client.post(
            _ROUTE_LOGIN_MFA,
            json={"mfa_challenge_id": generate_token(), "totp_code": _current_code(secret)},
        )
        expired = await client.post(
            _ROUTE_LOGIN_MFA, json={"mfa_challenge_id": cid, "totp_code": _current_code(secret)}
        )

    assert unknown.status_code == expired.status_code == 401
    assert unknown.json() == expired.json() == _MFA_INVALID_401
    assert unknown.content == expired.content  # 统一文案逐字节一致
    assert await _count(pg, "sessions") == 0
    # 未知/过期挑战不写 mfa_failure 限流事件（尝试计数归挑战存储）
    assert await _count(pg, "rate_limit_events", "scope = 'mfa_failure'") == 0


# ---------- login/mfa：5 次耗尽销毁挑战 + mfa_failure 事件 ----------


async def test_mfa_five_wrong_codes_destroy_challenge_and_write_events(pg, flow_env):
    secret = pyotp.random_base32()
    uid = await _seed_user(pg, "mfa-lock@example.com", password="pw-lock", totp_secret=secret)
    cid = await _login_challenge("mfa-lock@example.com", "pw-lock")
    wrong = _wrong_code(secret)
    bodies = []
    async with http_client("10.0.31.2") as client:
        for _ in range(5):
            resp = await client.post(
                _ROUTE_LOGIN_MFA, json={"mfa_challenge_id": cid, "totp_code": wrong}
            )
            assert resp.status_code == 401
            assert resp.json() == _MFA_INVALID_401
            bodies.append(resp.content)
        # 第 6 次：挑战已销毁，即便持有正确码也统一 401
        sixth = await client.post(
            _ROUTE_LOGIN_MFA, json={"mfa_challenge_id": cid, "totp_code": _current_code(secret)}
        )

    assert sixth.status_code == 401
    assert sixth.json() == _MFA_INVALID_401
    assert len(set(bodies + [sixth.content])) == 1  # 六次响应体逐字节一致
    assert cid not in login_service.challenge_store._entries  # 挑战确已销毁
    assert await _count(pg, "sessions") == 0
    # 每次码错写一行 mfa_failure 事件（subject [HMAC(user_id)]；第 6 次未达挑战不写）
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'mfa_failure' AND subject_hash = :h",
        {"h": hmac_subject("user", uid)},
    )
    assert n == 5


# ---------- login/mfa：mfa_failure 第 11 次失败 → 429 替换 401 ----------


async def test_mfa_failure_11th_returns_429_replacing_401(pg, flow_env):
    """mfa_failure 上限（10/900s）三项契约（brief Step 1 点名）：

    (a) 同用户第 11 次失败 → 429 TOO_MANY_REQUESTS（**替换**统一 401——429 wins）；
    (b) 429 带 Retry-After；
    (c) enforce 先于 attempts 递增——429 那次的挑战 attempts 不含该次（未推进），
        且拒绝路径不写限流事件（mfa_failure 恰 10 行）。

    构造（无睡眠）：3 次登录各取挑战（每挑战至多 4 次错码防 5 次销毁；login 主体
    3 事件低于 10 上限）→ 4+4+2 次错码均 401（事件 1-10）→ 第 11 次错码 429。
    """
    secret = pyotp.random_base32()
    uid = await _seed_user(pg, "mfa-rl@example.com", password="pw-rl", totp_secret=secret)
    wrong = _wrong_code(secret)
    cid1 = await _login_challenge("mfa-rl@example.com", "pw-rl")
    cid2 = await _login_challenge("mfa-rl@example.com", "pw-rl")
    cid3 = await _login_challenge("mfa-rl@example.com", "pw-rl")

    async with http_client("10.0.31.12") as client:

        async def _fail(challenge_id: str) -> httpx.Response:
            return await client.post(
                _ROUTE_LOGIN_MFA, json={"mfa_challenge_id": challenge_id, "totp_code": wrong}
            )

        first = [await _fail(cid1) for _ in range(4)]  # 事件 1-4
        second = [await _fail(cid2) for _ in range(4)]  # 事件 5-8
        third = [await _fail(cid3) for _ in range(3)]  # 事件 9-10 + 第 11 次 429

    for resp in (*first, *second, *third[:2]):
        assert resp.status_code == 401
        assert resp.json() == _MFA_INVALID_401

    eleventh = third[2]
    assert eleventh.status_code == 429  # (a) 429 替换 401
    assert eleventh.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert 1 <= int(eleventh.headers["Retry-After"]) <= 900  # (b)

    assert login_service.challenge_store._entries[cid3].attempts == 2  # (c) 429 未推进
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'mfa_failure' AND subject_hash = :h",
        {"h": hmac_subject("user", uid)},
    )
    assert n == 10  # 拒绝路径不写事件
    # 3 次登录各写一行（email 主体），低于 login 10/900 上限未触发
    n_login = await _count(
        pg,
        "rate_limit_events",
        "scope = 'login' AND subject_hash = :h",
        {"h": hmac_subject("email", "mfa-rl@example.com")},
    )
    assert n_login == 3


# ---------- login/mfa：正确码 → 会话（mfa_verified_at 非空）----------


async def test_mfa_correct_code_creates_session_with_mfa_verified(pg, flow_env):
    secret = pyotp.random_base32()
    uid = await _seed_user(pg, "mfa-ok@example.com", password="pw-ok", totp_secret=secret)
    cid = await _login_challenge("mfa-ok@example.com", "pw-ok")
    async with http_client("10.0.31.3") as client:
        resp = await client.post(
            _ROUTE_LOGIN_MFA, json={"mfa_challenge_id": cid, "totp_code": _current_code(secret)}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["user"]["id"] == uid
    assert body["data"]["csrf_token"]
    row = await _one(pg, "SELECT mfa_verified_at FROM sessions WHERE user_id = :u", {"u": uid})
    assert row["mfa_verified_at"] is not None  # MFA 验证会话
    assert set(_cookie_jar(resp)) == {COOKIE_NAME, CSRF_COOKIE_NAME}
    assert cid not in login_service.challenge_store._entries  # 成功即销毁（一次性）


# ---------- setup → activate 全链 ----------


async def test_mfa_setup_activate_full_chain(pg, flow_env):
    async with authenticated_client(
        pg, flow_env, email="enroll@example.com", status="active", ip="10.0.31.4"
    ) as (client, csrf, uid):
        setup = await client.post(_ROUTE_MFA_SETUP, headers={"X-CSRF-Token": csrf})
        assert setup.status_code == 200
        secret = setup.json()["data"]["secret"]
        uri = setup.json()["data"]["otpauth_uri"]
        assert uri.startswith("otpauth://totp/AgentCraft:")
        assert "issuer=AgentCraft" in uri and "enroll%40example.com" in uri

        activate = await client.post(
            _ROUTE_MFA_ACTIVATE,
            json={"totp_code": _current_code(secret)},
            headers={"X-CSRF-Token": csrf},
        )

    assert activate.status_code == 200
    assert activate.json() == {"data": {"mfa_enabled": True}}

    # users.mfa_secret_enc 可用同材料 keyring + AAD 解回原 secret
    row = await _one(pg, "SELECT mfa_secret_enc FROM users WHERE id = :u", {"u": uid})
    _, keyring = make_keyring(f"primary:{_KEY_MATERIAL}")
    plain = decrypt_text(
        json.loads(row["mfa_secret_enc"]),
        aad=login_service.mfa_secret_aad(str(uid)),
        keyring=keyring,
    )
    assert plain == secret
    # 当前会话盖 mfa_verified_at
    srow = await _one(pg, "SELECT mfa_verified_at FROM sessions WHERE user_id = :u", {"u": uid})
    assert srow["mfa_verified_at"] is not None
    # pending secret 注册成功即消费
    assert str(uid) not in login_service.pending_mfa_secrets._entries


async def test_mfa_setup_twice_overwrites_pending_secret(pg, flow_env):
    async with authenticated_client(
        pg, flow_env, email="re-setup@example.com", status="active", ip="10.0.31.5"
    ) as (client, csrf, uid):
        first = await client.post(_ROUTE_MFA_SETUP, headers={"X-CSRF-Token": csrf})
        second = await client.post(_ROUTE_MFA_SETUP, headers={"X-CSRF-Token": csrf})

    assert first.status_code == second.status_code == 200
    s1 = first.json()["data"]["secret"]
    s2 = second.json()["data"]["secret"]
    assert s1 != s2
    assert login_service.pending_mfa_secrets._entries[str(uid)].secret == s2  # 覆盖
    assert await _count(pg, "users", "mfa_secret_enc IS NOT NULL") == 0  # setup 不落库


async def test_mfa_activate_without_setup_400(pg, flow_env):
    async with authenticated_client(
        pg, flow_env, email="nosetup@example.com", status="active", ip="10.0.31.6"
    ) as (client, csrf, _uid):
        resp = await client.post(
            _ROUTE_MFA_ACTIVATE, json={"totp_code": "123456"}, headers={"X-CSRF-Token": csrf}
        )

    assert resp.status_code == 400
    assert resp.json() == _MFA_INVALID_400
    assert await _count(pg, "users", "mfa_secret_enc IS NOT NULL") == 0


async def test_mfa_activate_wrong_code_400_and_mfa_failure_event(pg, flow_env):
    async with authenticated_client(
        pg, flow_env, email="act-fail@example.com", status="active", ip="10.0.31.7"
    ) as (client, csrf, uid):
        secret = (await client.post(_ROUTE_MFA_SETUP, headers={"X-CSRF-Token": csrf})).json()[
            "data"
        ]["secret"]
        resp = await client.post(
            _ROUTE_MFA_ACTIVATE,
            json={"totp_code": _wrong_code(secret)},
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 400
    assert resp.json() == _MFA_INVALID_400
    assert await _count(pg, "users", "mfa_secret_enc IS NOT NULL") == 0  # 未落库
    assert str(uid) in login_service.pending_mfa_secrets._entries  # pending 保留可重试
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'mfa_failure' AND subject_hash = :h",
        {"h": hmac_subject("user", str(uid))},
    )
    assert n == 1  # A15：activate 失败走 mfa_failure 主体 [HMAC(user_id)]


# ---------- disable：admin 403（A9）/ user 200 ----------


async def test_mfa_disable_admin_forbidden_user_ok(pg, flow_env):
    admin_uid = await _seed_user(pg, "admin-mfa@example.com", password="admin-pw", role="admin")
    async with owner_session(flow_env, admin_uid) as db:
        session_token, csrf = await create_session(
            db, user_id=uuid.UUID(admin_uid), device_label="AgentCraft-FlowTest/1.0"
        )
    async with http_client("10.0.31.8", cookies={"ac_session": session_token}) as client:
        admin_resp = await client.delete(_ROUTE_MFA_DISABLE, headers={"X-CSRF-Token": csrf})

    assert admin_resp.status_code == 403
    assert admin_resp.json() == _FORBIDDEN_403

    secret = pyotp.random_base32()
    uid = await _seed_user(pg, "disable@example.com", password="user-pw", totp_secret=secret)
    async with owner_session(flow_env, uid) as db:
        session_token2, csrf2 = await create_session(
            db, user_id=uuid.UUID(uid), device_label="AgentCraft-FlowTest/1.0"
        )
    async with http_client("10.0.31.9", cookies={"ac_session": session_token2}) as client:
        user_resp = await client.delete(_ROUTE_MFA_DISABLE, headers={"X-CSRF-Token": csrf2})

    assert user_resp.status_code == 200
    assert user_resp.json() == {"data": {"mfa_enabled": False}}
    row = await _one(pg, "SELECT mfa_secret_enc FROM users WHERE id = :u", {"u": uid})
    assert row["mfa_secret_enc"] is None


# ---------- login 限流：同账户+IP 第 11 次 429 ----------


async def test_login_rate_limit_11th_attempt_429_with_retry_after(pg, flow_env):
    await _seed_user(pg, "rl-login@example.com", password="rl-pw")
    payload = {"email": "rl-login@example.com", "password": "bad"}
    async with http_client("10.0.31.10") as client:
        for _ in range(10):
            resp = await client.post(_ROUTE_LOGIN, json=payload)
            assert resp.status_code == 401
        eleventh = await client.post(_ROUTE_LOGIN, json=payload)

    assert eleventh.status_code == 429
    assert eleventh.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert 1 <= int(eleventh.headers["Retry-After"]) <= 900
    # 每次尝试两行事件（email + ip 主体各一）；拒绝路径不写
    n_email = await _count(
        pg,
        "rate_limit_events",
        "scope = 'login' AND subject_hash = :h",
        {"h": hmac_subject("email", "rl-login@example.com")},
    )
    n_ip = await _count(
        pg,
        "rate_limit_events",
        "scope = 'login' AND subject_hash = :h",
        {"h": hmac_subject("ip", "10.0.31.10")},
    )
    assert n_email == 10 and n_ip == 10


# ---------- schema 边界：零 DB 副作用 ----------


async def test_login_schema_bounds_zero_side_effects(pg, flow_env):
    async with http_client("10.0.31.11") as client:
        big_password = await client.post(
            _ROUTE_LOGIN, json={"email": "a@b.test", "password": "x" * 1025}
        )
        short_code = await client.post(
            _ROUTE_LOGIN_MFA, json={"mfa_challenge_id": "c", "totp_code": "12345"}
        )

    assert big_password.status_code == 400
    assert big_password.json()["error"]["code"] == "VALIDATION_ERROR"
    assert short_code.status_code == 400
    assert short_code.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await _count(pg, "rate_limit_events") == 0  # schema 门先于限流与业务
    assert await _count(pg, "sessions") == 0
