"""step-up MFA 续期端点端到端测试（Phase 8 T3）。

契约出处：task-3-brief + Supplement §10.4（D7）。

- POST /api/auth/mfa/verify（实现期挂载；T14 后切契约路径 /api/auth/mfa/verify）：
  认证端点（get_v2_auth 门序：会话 cookie → CSRF → 状态门），普通用户即可（非
  admin 门）；载荷 {totp_code}。
- 正确码 → 200 {data:{mfa_verified:true}} 且 sessions.mfa_verified_at=now()（12h
  窗重置，admin 门③即刻解除）；未配置 TOTP → 400 MFA_NOT_CONFIGURED（已认证无
  枚举面，与 login 侧统一 401 不对称——§10.4 注记）；错误码 → 401 MFA_INVALID 且
  mfa_failure 限流（(10, 900s) 滑窗，第 11 次失败 429+Retry-After——verify 无挑战
  条目，与 login/mfa 挑战 5 次尝试是两套机制，勿混）。
- 幂等豁免（§10.4）：同 body 重发不产生 idempotency_records 行（路由不接幂等
  begin，响应不入幂等记录）。
- HTTP 层沿用 house pattern：httpx.AsyncClient(ASGITransport(app, client=(ip,
  port))) 于测试自身循环驱动真实 app；会话以 superuser 播种 + owner 事务
  create_session 构造（cookie 明文经构造参数直投 jar，免 SESSION_COOKIE_SECURE
  依赖，T11 house pattern 同款）。
- admin 12h 门联动例复用 tests/v2_admin_helpers.py 的 admin_env（三件套注入 + 双
  role runtime override）；admin 用户/会话由本文件自播种（TOTP secret 需在手，
  helpers.admin_client 不外泄 secret）。
- 密钥注入：RATE_LIMIT_HMAC_KEY / MFA_ENCRYPTION_KEY 每次现读 Settings，经
  monkeypatch.setenv 注入一次性 b64url 材料（test_v2_login_mfa.flow_env 同款）；
  TOTP 种子用户由测试以同材料 keyring 直接构造信封写 mfa_secret_enc。
"""

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from sqlalchemy import text

from backend.main import app
from backend.utils.crypto import encrypt_text, make_keyring
from backend.v2 import login_service
from backend.v2.rate_limit import hmac_subject
from backend.v2.runtime import get_v2_runtime, owner_session
from backend.v2.security import hash_password
from backend.v2.session_service import COOKIE_NAME, create_session
from tests import v2_admin_helpers as _vah
from tests.conftest import PgDb
from tests.test_v2_runtime import make_v2_runtime
from tests.test_v2_verification_flow import http_client

# admin_env 夹具发现形态：pytest 经模块命名空间发现夹具，但「import + 参数同名」
# 会触发 ruff F811（参数遮蔽未使用的 import）——以赋值别名引入（test_v2_admin_deps 同型）。
admin_env = _vah.admin_env

_ROUTE_VERIFY = "/api/auth/mfa/verify"
_ROUTE_ADMIN_INVITATIONS = "/api/admin/invitations"
_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()  # 仅测试材料
_UA = "AgentCraft-StepupTest/1.0"

# 统一失败文案（契约钉死；响应体形状见 backend/main.py 错误处理器）
_MFA_INVALID_401 = {"error": {"code": "MFA_INVALID", "message": "验证码无效或已过期"}}
_SESSION_EXPIRED_401 = {
    "error": {"code": "SESSION_EXPIRED", "message": "登录状态已失效，请重新登录"}
}


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


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


def _mfa_envelope(secret: str, user_id: str) -> str:
    """测试 keyring 直接构造信封（材料与 flow_env/admin_env 注入 Settings 的同源）。"""
    _, keyring = make_keyring(f"primary:{_KEY_MATERIAL}")
    envelope = encrypt_text(
        secret, aad=login_service.mfa_secret_aad(user_id), keyring=keyring, active_kid="primary"
    )
    return json.dumps(envelope)


async def _seed_user(
    pg: PgDb,
    email: str,
    *,
    role: str = "user",
    totp_secret: str | None = None,
) -> str:
    """播种 active 用户行（可带 TOTP 信封；admin 形态供 12h 门联动例），返回 user id。"""
    new_id = uuid.uuid4()
    enc = _mfa_envelope(totp_secret, str(new_id)) if totp_secret is not None else None
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc) "
                "VALUES (:i, :e, :p, :r, 'active', :m)"
            ),
            {
                "i": str(new_id),
                "e": email,
                "p": hash_password("pw-123456"),
                "r": role,
                "m": enc,
            },
        )
    return str(new_id)


async def _new_session(rt, user_id: str, *, mfa_verified: bool) -> tuple[str, str]:
    """owner 事务内建会话（INSERT 满足 sessions_app_insert WITH CHECK）；返回 (token, csrf)。"""
    async with owner_session(rt, user_id) as db:
        return await create_session(
            db, user_id=uuid.UUID(user_id), device_label=_UA, mfa_verified=mfa_verified
        )


async def _backdate_mfa_verified(pg: PgDb, user_id: str, *, hours: int) -> None:
    """superuser 回拨会话 mfa_verified_at（sessions owner-RLS，superuser 绕过）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET mfa_verified_at = now() - make_interval(secs => :s) "
                "WHERE user_id = :u"
            ),
            {"s": hours * 3600, "u": user_id},
        )


async def _count(pg: PgDb, table: str, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {})
        ).scalar_one()


async def _one(pg: PgDb, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


def _current_code(secret: str) -> str:
    """当前 30s 窗口的有效 TOTP 码（pyotp .at 免睡眠，house pattern 钉死）。"""
    return pyotp.TOTP(secret).at(datetime.now(timezone.utc))


def _wrong_code(secret: str) -> str:
    """与当前时间码必然不同的 6 位码（排除万年一遇的假阴性）。"""
    current = _current_code(secret)
    return "000000" if current != "000000" else "000001"


# ---------- ① 正确码 → 200 + mfa_verified_at 刷新为 now ----------


async def test_verify_correct_code_refreshes_mfa_verified_at(pg, flow_env):
    """正确码 200 {data:{mfa_verified:true}}；回拨 13h 的过期持有窗刷新为 now。

    前置态取 step-up 真实场景：会话带 13h 前的 mfa_verified_at（activate 盖戳后
    窗口已过）。成功响应不种任何 cookie（非重登录，会话行原样续用）。
    """
    rt = flow_env
    secret = pyotp.random_base32()
    uid = await _seed_user(pg, "stepup-ok@example.com", totp_secret=secret)
    token, csrf = await _new_session(rt, uid, mfa_verified=False)
    await _backdate_mfa_verified(pg, uid, hours=13)

    async with http_client("10.0.33.1", cookies={COOKIE_NAME: token}) as client:
        resp = await client.post(
            _ROUTE_VERIFY,
            json={"totp_code": _current_code(secret)},
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 200
    assert resp.json() == {"data": {"mfa_verified": True}}
    assert resp.headers.get_list("set-cookie") == []  # 非重登录：不种会话/CSRF cookie

    row = await _one(pg, "SELECT mfa_verified_at FROM sessions WHERE user_id = :u", {"u": uid})
    assert row["mfa_verified_at"] is not None
    assert abs(datetime.now(timezone.utc) - row["mfa_verified_at"]) < timedelta(minutes=1)


# ---------- ② 错误码 → 401 MFA_INVALID + mfa_failure 限流（第 11 次 429）----------


async def test_verify_wrong_code_401_and_11th_failure_429(pg, flow_env):
    """mfa_failure 滑窗（10/900s，主体 [HMAC(user)]）三项契约：

    (a) 前 10 次错码 → 401 MFA_INVALID（逐次写限流事件）；(b) 第 11 次失败 → 429
    TOO_MANY_REQUESTS 替换 401 + Retry-After；(c) 拒绝路径不写事件（恰 10 行），
    会话 mfa_verified_at 不被错误码推进（保持 NULL）。verify 无挑战条目——不涉
    login/mfa 的挑战 5 次尝试机制。
    """
    secret = pyotp.random_base32()
    uid = await _seed_user(pg, "stepup-rl@example.com", totp_secret=secret)
    token, csrf = await _new_session(flow_env, uid, mfa_verified=False)
    wrong = _wrong_code(secret)

    async with http_client("10.0.33.2", cookies={COOKIE_NAME: token}) as client:
        responses = [
            await client.post(
                _ROUTE_VERIFY, json={"totp_code": wrong}, headers={"X-CSRF-Token": csrf}
            )
            for _ in range(11)
        ]

    for resp in responses[:10]:
        assert resp.status_code == 401
        assert resp.json() == _MFA_INVALID_401

    eleventh = responses[10]
    assert eleventh.status_code == 429  # (b) 429 替换 401
    assert eleventh.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert 1 <= int(eleventh.headers["Retry-After"]) <= 900

    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'mfa_failure' AND subject_hash = :h",
        {"h": hmac_subject("user", uid)},
    )
    assert n == 10  # (c) 拒绝路径不写事件
    row = await _one(pg, "SELECT mfa_verified_at FROM sessions WHERE user_id = :u", {"u": uid})
    assert row["mfa_verified_at"] is None


# ---------- ③ 未配置 TOTP → 400 MFA_NOT_CONFIGURED ----------


async def test_verify_without_totp_400_mfa_not_configured(pg, flow_env):
    """mfa_secret_enc NULL → 400 MFA_NOT_CONFIGURED（§10.4 不对称注记：已认证无
    枚举面，与 login 侧统一 401 防枚举不同）；不计失败限流、不动会话戳。"""
    uid = await _seed_user(pg, "stepup-none@example.com")  # 未配置 TOTP
    token, csrf = await _new_session(flow_env, uid, mfa_verified=False)

    async with http_client("10.0.33.3", cookies={COOKIE_NAME: token}) as client:
        resp = await client.post(
            _ROUTE_VERIFY,
            json={"totp_code": _current_code(pyotp.random_base32())},
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "MFA_NOT_CONFIGURED"
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'mfa_failure' AND subject_hash = :h",
        {"h": hmac_subject("user", uid)},
    )
    assert n == 0  # 未配置不计失败（非码错路径）
    row = await _one(pg, "SELECT mfa_verified_at FROM sessions WHERE user_id = :u", {"u": uid})
    assert row["mfa_verified_at"] is None


# ---------- ④ 未认证 → 401 SESSION_EXPIRED ----------


async def test_verify_unauthenticated_401_session_expired(pg, flow_env):
    """无会话 cookie → get_v2_auth 门序统一 401 SESSION_EXPIRED（公开面零信息）。"""
    async with http_client("10.0.33.4") as client:
        resp = await client.post(_ROUTE_VERIFY, json={"totp_code": "123456"})

    assert resp.status_code == 401
    assert resp.json() == _SESSION_EXPIRED_401


# ---------- ⑤ 12h 门联动：verify 成功后 admin 门③即刻解除 ----------


async def test_verify_reopens_admin_12h_gate(pg, admin_env):
    """admin 门③联动（§10.4：12h 窗重置，admin 门③即刻解除）：

    admin + TOTP 已配置 + mfa_verified 会话回拨 13h → admin 读端点 403
    ADMIN_MFA_REQUIRED → verify 正确码 200 → 同会话再探 admin 读端点 200（三重门
    读 get_v2_auth 每请求新解析的会话快照，owner UPDATE 落库后下一请求即见新戳，
    无需重登录）。门②不动：未配置 TOTP 者仍永不过门。
    """
    rt = admin_env
    secret = pyotp.random_base32()
    admin_id = await _seed_user(pg, "stepup-admin@example.com", role="admin", totp_secret=secret)
    token, csrf = await _new_session(rt, admin_id, mfa_verified=True)
    await _backdate_mfa_verified(pg, admin_id, hours=13)

    async with http_client("10.0.33.5", cookies={COOKIE_NAME: token}) as client:
        denied = await client.get(_ROUTE_ADMIN_INVITATIONS)
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"

        renewed = await client.post(
            _ROUTE_VERIFY,
            json={"totp_code": _current_code(secret)},
            headers={"X-CSRF-Token": csrf},
        )
        assert renewed.status_code == 200
        assert renewed.json() == {"data": {"mfa_verified": True}}

        passed = await client.get(_ROUTE_ADMIN_INVITATIONS)
        assert passed.status_code == 200
        assert passed.json()["data"]["total"] == 0  # 门③解除：空目录列表放行


# ---------- ⑥ 幂等豁免：同 body 重发不产生 idempotency_records 行 ----------


async def test_verify_idempotency_exempt_no_idempotency_records(pg, flow_env):
    """§10.4 豁免清单路径：同 body 重发两次均 200（同 30s 窗内码仍有效），幂等
    记录零行——路由不接幂等 begin，响应不入幂等记录（重复提交由 mfa_failure
    限流兜底）。"""
    secret = pyotp.random_base32()
    uid = await _seed_user(pg, "stepup-idem@example.com", totp_secret=secret)
    token, csrf = await _new_session(flow_env, uid, mfa_verified=False)
    body = {"totp_code": _current_code(secret)}

    async with http_client("10.0.33.6", cookies={COOKIE_NAME: token}) as client:
        first = await client.post(_ROUTE_VERIFY, json=body, headers={"X-CSRF-Token": csrf})
        second = await client.post(_ROUTE_VERIFY, json=body, headers={"X-CSRF-Token": csrf})

    assert first.status_code == 200
    assert second.status_code == 200
    assert await _count(pg, "idempotency_records") == 0
