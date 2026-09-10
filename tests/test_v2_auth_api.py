"""会话面（logout/设备列表/删除）与 users/me 端到端测试（Task 14）。

契约出处：task-14-brief + Supplement §2/§7 + 裁决 A5/A12/A14；端点契约详见
backend/api/v2/auth.py / account.py 路由 docstring，此处不重复。覆盖要点：

- logout（认证+CSRF，豁免幂等 A5）：撤销当前会话 + 双 cookie 清除 + 200 ok；
  同一死 cookie 复用 → 401 SESSION_EXPIRED（自然形态，无特殊处理，钉死）。
- GET sessions（认证，GET 免 CSRF）：{data, total, page, size} 列表信封；
  current 标志恰一；id/created_at/expires_at JSON 可序列化（ISO 形态）。
- DELETE sessions/{id}（认证+CSRF+Idempotency-Key 必带，subject=user、route=含
  资源 ID 的具体路径）：幂等 begin 最先（§7 重放优先——同 key 已成功删除的重放
  返回原 200 且不携带 Set-Cookie，新 key 对已撤销会话则 404）；rowcount 0 →
  统一 404「资源不存在」（他人会话/不存在/已撤销一律 404，RLS 保证绝不 403
  泄漏存在性）；撤销当前会话时同时清双 cookie。
- users/me（认证，GET 免 CSRF）：形状钉死（id/email/role/status/entitlements/
  mfa_enabled）；entitlements 仅 revoked_at IS NULL 生效行；mfa_enabled =
  mfa_secret_enc IS NOT NULL；不返回 csrf_token（A14）。
- 横切验收（Eng §7 门槛）：会话固定 A12（login/accept 前预置伪造 cookie → 全新
  cookie、伪造令牌无行、既有会话不受影响）；CSRF 横切（logout/DELETE 缺头/错头
  403，GET 免检）；限流重启连续性（enforce 落库后全新 runtime 仍计数）；
  cookie 属性矩阵（ac_session HttpOnly / ac_csrf 非 HttpOnly，其余一致）；
  401 SESSION_EXPIRED 失效形态逐字节一致。

HTTP 层沿用 T10-T13 house pattern（ASGITransport 于测试自身循环驱动真实 app）；
助手复用 T10/T12（http_client/_auth_client/_seed_user 等，真实 Argon2 哈希）与
T9（_seed_invitation）。种子/复核一律 superuser（pg.engine，绕 owner-RLS）。
密钥注入：RATE_LIMIT_HMAC_KEY（login/accept 限流主体）+ EMAIL_OUTBOX_ENCRYPTION_KEY
（accept outbox 入队）经 monkeypatch.setenv 注入一次性 b64url 材料。
"""

import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.main import app
from backend.v2.rate_limit import enforce
from backend.v2.runtime import get_v2_runtime, owner_session
from backend.v2.security import generate_token, hash_token
from backend.v2.session_service import create_session
from tests.conftest import PgDb
from tests.test_v2_invitation_flow import _seed_invitation
from tests.test_v2_password_flows import (
    _KEY_MATERIAL,
    _auth_client,
    _count,
    _extra_session,
    _one,
    _seed_user,
    _session_id_by_token,
    _session_revoked,
    http_client,
)
from tests.test_v2_runtime import make_v2_runtime

_ROUTE_LOGOUT = "/api/v2/auth/logout"
_ROUTE_SESSIONS = "/api/v2/auth/sessions"
_ROUTE_ME = "/api/v2/users/me"
_ROUTE_ACCEPT = "/api/v2/auth/invitations/accept"
_ROUTE_LOGIN = "/api/v2/auth/login"
_UA = "AgentCraft-FlowTest/1.0"

# 统一成功/失败载荷（契约钉死；响应体形状见 backend/main.py 错误处理器）
_OK_200 = {"data": {"ok": True}}
_NOT_FOUND_404 = {"error": {"code": "NOT_FOUND", "message": "资源不存在"}}
_SESSION_EXPIRED_401 = {
    "error": {"code": "SESSION_EXPIRED", "message": "登录状态已失效，请重新登录"}
}
_CSRF_403 = {"error": {"code": "CSRF_INVALID", "message": "CSRF 校验失败"}}


@pytest.fixture
async def flow_env(pg: PgDb, monkeypatch):
    """app/admin 双 role runtime + FastAPI 依赖 override + 双密钥注入；yield runtime。

    与 T10 flow_env 同款（材料常量同源）；独立定义以避免测试模块间 fixture 名
    re-export 的遮蔽歧义。
    """
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("EMAIL_OUTBOX_ENCRYPTION_KEY", _KEY_MATERIAL)
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield rt
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.close()  # 同步 dispose 双引擎连接池（sync_engine.dispose）


def _cookie_value(resp: httpx.Response, name: str) -> str:
    """从 Set-Cookie 头提取指定 cookie 明文（SESSION_COOKIE_SECURE 下 jar 不持久化）。"""
    header = next(c for c in resp.headers.get_list("set-cookie") if c.startswith(f"{name}="))
    return header.split("=", 1)[1].split(";", 1)[0]


# ---------- logout：撤销 + 清 cookie + 死 cookie 自然 401 ----------


async def test_logout_revokes_session_and_clears_cookies(pg, flow_env):
    async with _auth_client(pg, flow_env, email="lo@example.com", ip="10.0.70.1") as (
        client,
        csrf,
        uid,
        token,
    ):
        resp = await client.post(_ROUTE_LOGOUT, headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 200
    assert resp.json() == _OK_200
    assert len(resp.headers.get_list("set-cookie")) == 2  # 双 cookie 一并清除
    assert await _session_revoked(pg, token)

    # 同一死 cookie 再登出 → 401 SESSION_EXPIRED（自然形态，无特殊处理，契约钉死）
    async with http_client("10.0.70.1", cookies={"ac_session": token}) as again:
        replay = await again.post(_ROUTE_LOGOUT)
    assert replay.status_code == 401
    assert replay.json() == _SESSION_EXPIRED_401


# ---------- logout / DELETE：CSRF 横切（缺头/错头 403，先于幂等与业务）----------


async def test_logout_without_or_wrong_csrf_403(pg, flow_env):
    async with _auth_client(pg, flow_env, email="lo-csrf@example.com", ip="10.0.70.2") as (
        client,
        csrf,
        uid,
        token,
    ):
        missing = await client.post(_ROUTE_LOGOUT)  # 无 X-CSRF-Token
        wrong = await client.post(_ROUTE_LOGOUT, headers={"X-CSRF-Token": "wrong-token"})

    assert missing.status_code == wrong.status_code == 403
    assert missing.json() == wrong.json() == _CSRF_403
    assert not await _session_revoked(pg, token)  # 零副作用


async def test_delete_session_without_or_wrong_csrf_403_zero_side_effects(pg, flow_env):
    async with _auth_client(pg, flow_env, email="del-csrf@example.com", ip="10.0.70.3") as (
        client,
        csrf,
        uid,
        token,
    ):
        other_token, _ = await _extra_session(flow_env, uid)
        other_id = await _session_id_by_token(pg, other_token)
        missing = await client.delete(
            f"{_ROUTE_SESSIONS}/{other_id}", headers={"Idempotency-Key": "csrf-miss"}
        )
        wrong = await client.delete(
            f"{_ROUTE_SESSIONS}/{other_id}",
            headers={"X-CSRF-Token": "nope", "Idempotency-Key": "csrf-wrong"},
        )

    assert missing.status_code == wrong.status_code == 403
    assert missing.json() == wrong.json() == _CSRF_403
    assert not await _session_revoked(pg, other_token)
    assert await _count(pg, "idempotency_records") == 0  # get_v2_auth 门先于幂等 begin


# ---------- GET sessions：列表信封形状 + current 标志（GET 免 CSRF）----------


async def test_sessions_list_envelope_shape_and_current_flag(pg, flow_env):
    async with _auth_client(pg, flow_env, email="list@example.com", ip="10.0.70.4") as (
        client,
        csrf,
        uid,
        current_token,
    ):
        await _extra_session(flow_env, uid, device="AgentCraft-Other/1.0")
        resp = await client.get(_ROUTE_SESSIONS)  # 无 X-CSRF-Token：GET 免 CSRF

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == body["size"] == 2 and body["page"] == 1
    items = body["data"]
    assert len(items) == 2
    assert {i["device_label"] for i in items} == {_UA, "AgentCraft-Other/1.0"}
    currents = [i for i in items if i["current"]]
    assert len(currents) == 1  # current 标志恰一
    assert currents[0]["id"] == await _session_id_by_token(pg, current_token)
    for i in items:
        assert set(i) == {"id", "device_label", "created_at", "expires_at", "current"}
        assert "T" in i["created_at"] and "T" in i["expires_at"]  # ISO 序列化形态


# ---------- DELETE sessions/{id}：统一 404 矩阵（他人/不存在/已撤销，绝不 403）----------


async def test_delete_session_uniform_404_matrix(pg, flow_env):
    async with _auth_client(pg, flow_env, email="del-404@example.com", ip="10.0.70.5") as (
        client,
        csrf,
        uid,
        token,
    ):
        foreign_uid = await _seed_user(pg, "victim@example.com", password="pw-victim")
        foreign_token, _ = await _extra_session(flow_env, foreign_uid)
        foreign_id = await _session_id_by_token(pg, foreign_token)
        rev_token, _ = await _extra_session(flow_env, uid)
        rev_id = await _session_id_by_token(pg, rev_token)
        prep = await client.delete(
            f"{_ROUTE_SESSIONS}/{rev_id}",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "d404-prep"},
        )
        assert prep.status_code == 200

        cases = [
            ("foreign_session", foreign_id, "d404-a"),
            ("missing_id", uuid.uuid4(), "d404-b"),
            ("already_revoked", rev_id, "d404-c"),  # 新 key 对已撤销会话仍 404
        ]
        bodies = []
        for mode, sid, key in cases:
            resp = await client.delete(
                f"{_ROUTE_SESSIONS}/{sid}",
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": key},
            )
            assert resp.status_code == 404, mode
            assert resp.json() == _NOT_FOUND_404, mode
            bodies.append(resp.content)
        assert len(set(bodies)) == 1  # 统一 404：响应体逐字节一致（防存在性探测）

    assert not await _session_revoked(pg, foreign_token)  # 他人会话分文未动


async def test_delete_current_session_revokes_and_clears_cookies(pg, flow_env):
    async with _auth_client(pg, flow_env, email="del-cur@example.com", ip="10.0.70.6") as (
        client,
        csrf,
        uid,
        token,
    ):
        current_id = await _session_id_by_token(pg, token)
        resp = await client.delete(
            f"{_ROUTE_SESSIONS}/{current_id}",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "del-current"},
        )

    assert resp.status_code == 200
    assert resp.json() == _OK_200
    assert len(resp.headers.get_list("set-cookie")) == 2  # 撤销当前会话：双 cookie 清除
    assert await _session_revoked(pg, token)
    rec = await _one(pg, "SELECT status_code, response_json, route FROM idempotency_records")
    assert rec["status_code"] == 200 and rec["response_json"] == _OK_200
    assert rec["route"] == f"/api/v2/auth/sessions/{current_id}"  # route 含资源 ID（A5）


async def test_delete_other_own_device_keeps_current_alive(pg, flow_env):
    async with _auth_client(pg, flow_env, email="del-other@example.com", ip="10.0.70.7") as (
        client,
        csrf,
        uid,
        current_token,
    ):
        other_token, _ = await _extra_session(flow_env, uid, device="AgentCraft-Other/1.0")
        other_id = await _session_id_by_token(pg, other_token)
        resp = await client.delete(
            f"{_ROUTE_SESSIONS}/{other_id}",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "del-other"},
        )
        assert resp.status_code == 200
        assert resp.json() == _OK_200
        assert resp.headers.get_list("set-cookie") == []  # 非当前会话：不清 cookie
        followup = await client.get(_ROUTE_SESSIONS)  # 当前会话立即可用（GET 免 CSRF）

    assert followup.status_code == 200 and followup.json()["total"] == 2  # 撤销行保留
    assert await _session_revoked(pg, other_token)
    assert not await _session_revoked(pg, current_token)


async def test_delete_session_requires_idempotency_key(pg, flow_env):
    async with _auth_client(pg, flow_env, email="del-nokey@example.com", ip="10.0.70.8") as (
        client,
        csrf,
        uid,
        token,
    ):
        other_token, _ = await _extra_session(flow_env, uid)
        other_id = await _session_id_by_token(pg, other_token)
        resp = await client.delete(f"{_ROUTE_SESSIONS}/{other_id}", headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert not await _session_revoked(pg, other_token)  # 零副作用
    assert await _count(pg, "idempotency_records") == 0


async def test_delete_session_replay_same_key_beats_404(pg, flow_env):
    """幂等 begin 最先（§7）：同 key 已成功删除的重放返回原 200（无论会话当前已
    撤销），且重放不携带 Set-Cookie；新 key 对同一已撤销会话则 404。"""
    async with _auth_client(pg, flow_env, email="del-replay@example.com", ip="10.0.70.9") as (
        client,
        csrf,
        uid,
        token,
    ):
        other_token, _ = await _extra_session(flow_env, uid, device="AgentCraft-Other/1.0")
        other_id = await _session_id_by_token(pg, other_token)
        path = f"{_ROUTE_SESSIONS}/{other_id}"
        first = await client.delete(
            path, headers={"X-CSRF-Token": csrf, "Idempotency-Key": "replay-del"}
        )
        assert first.status_code == 200
        fresh = await client.delete(
            path, headers={"X-CSRF-Token": csrf, "Idempotency-Key": "fresh-key"}
        )
        assert fresh.status_code == 404 and fresh.json() == _NOT_FOUND_404
        replay = await client.delete(
            path, headers={"X-CSRF-Token": csrf, "Idempotency-Key": "replay-del"}
        )

    assert replay.status_code == 200
    assert replay.json() == first.json() == _OK_200
    assert replay.headers.get_list("set-cookie") == []  # 重放不携带 Set-Cookie
    assert await _count(pg, "idempotency_records") == 1  # 重放不新增
    assert await _session_revoked(pg, other_token)


# ---------- users/me：形状钉死 + entitlements/mfa_enabled + 无 csrf_token（A14）----------


async def test_users_me_shape_without_entitlements_and_mfa(pg, flow_env):
    async with _auth_client(pg, flow_env, email="me@example.com", ip="10.0.70.10") as (
        client,
        csrf,
        uid,
        token,
    ):
        resp = await client.get(_ROUTE_ME)  # 无 X-CSRF-Token：GET 免 CSRF

    assert resp.status_code == 200
    assert resp.json() == {
        "data": {
            "id": uid,
            "email": "me@example.com",
            "role": "user",
            "status": "active",
            "entitlements": [],
            "mfa_enabled": False,
        }
    }
    assert "csrf_token" not in resp.text  # A14：服务端仅存 csrf_hash，无明文可还


async def test_users_me_entitlements_active_only_and_mfa_flag(pg, flow_env):
    async with _auth_client(pg, flow_env, email="me-ent@example.com", ip="10.0.70.11") as (
        client,
        csrf,
        uid,
        token,
    ):
        async with pg.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_entitlements (id, user_id, entitlement) "
                    "VALUES (gen_random_uuid(), :u, 'expert_author')"
                ),
                {"u": uid},
            )
            await conn.execute(
                text(
                    "INSERT INTO user_entitlements (id, user_id, entitlement, revoked_at) "
                    "VALUES (gen_random_uuid(), :u, 'expert_author', now())"
                ),
                {"u": uid},
            )
            await conn.execute(
                text("UPDATE users SET mfa_secret_enc = '{}' WHERE id = :u"), {"u": uid}
            )
        resp = await client.get(_ROUTE_ME)

    data = resp.json()["data"]
    assert data["entitlements"] == ["expert_author"]  # revoked_at 非空行被排除
    assert data["mfa_enabled"] is True  # mfa_secret_enc IS NOT NULL


# ---------- 会话固定（A12）：login/accept 前预置伪造 cookie ----------


async def test_session_fixation_forged_cookie_ignored_on_login(pg, flow_env):
    forged = generate_token()
    uid = await _seed_user(pg, "fix-login@example.com", password="pw-fix-1")
    async with owner_session(flow_env, uid) as db:
        keep_token, _ = await create_session(db, user_id=uuid.UUID(uid), device_label=_UA)

    async with http_client("10.0.70.12", cookies={"ac_session": forged}) as client:
        resp = await client.post(
            _ROUTE_LOGIN,
            json={"email": "fix-login@example.com", "password": "pw-fix-1"},
        )

    assert resp.status_code == 200
    assert _cookie_value(resp, "ac_session") != forged  # 全新 cookie，绝不复活旧值
    assert (
        await _count(pg, "sessions", "token_hash = :h", {"h": hash_token(forged)}) == 0
    )  # 伪造令牌无会话行
    assert not await _session_revoked(pg, keep_token)  # 既有有效会话不受影响


async def test_session_fixation_forged_cookie_ignored_on_accept(pg, flow_env):
    forged = generate_token()
    token, _iid = await _seed_invitation(pg, "fix-accept@example.com")

    async with http_client("10.0.70.13", cookies={"ac_session": forged}) as client:
        resp = await client.post(
            _ROUTE_ACCEPT,
            json={
                "invitation_token": token,
                "email": "fix-accept@example.com",
                "password": "s3cret-pw-123",
            },
            headers={"Idempotency-Key": "fix-accept-1"},
        )

    assert resp.status_code == 200
    assert _cookie_value(resp, "ac_session") != forged  # 全新 cookie（A12）
    assert await _count(pg, "sessions", "token_hash = :h", {"h": hash_token(forged)}) == 0


# ---------- 限流重启连续性（Eng §7）：enforce 落库后全新 runtime 仍计数 ----------


async def test_rate_limit_persistence_across_runtime_restart(pg):
    """enforce 写 rate_limit_events（自管事务独立提交）后销毁 runtime，再以全新
    双引擎 runtime（同 DB）enforce → 仍看见先前事件并 429（持久化而非进程内计数；
    服务级测试：绕过 HTTP 面，裸 app-role 会话直调 enforce）。"""
    subject = hash_token("rl-restart-subject")
    rt1 = make_v2_runtime(pg)
    try:
        for _ in range(3):  # email_verify_resend 限值 3：写满窗口
            async with rt1.app_factory() as db:
                await enforce(db, scope="email_verify_resend", subjects=[subject])
    finally:
        rt1.close()

    rt2 = make_v2_runtime(pg)  # 全新引擎连接池（模拟进程重启），同库
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with rt2.app_factory() as db:
                await enforce(db, scope="email_verify_resend", subjects=[subject])
    finally:
        rt2.close()

    assert exc_info.value.status_code == 429  # 重启后仍看见先前 3 行事件
    assert (
        await _count(
            pg,
            "rate_limit_events",
            "scope = 'email_verify_resend' AND subject_hash = :h",
            {"h": subject},
        )
        == 3
    )  # 拒绝路径不写


# ---------- cookie 属性矩阵（login 响应双 cookie）----------


async def test_cookie_attribute_matrix_on_login(pg, flow_env):
    await _seed_user(pg, "cookie-matrix@example.com", password="pw-matrix-1")
    async with http_client("10.0.70.14") as client:
        resp = await client.post(
            _ROUTE_LOGIN,
            json={"email": "cookie-matrix@example.com", "password": "pw-matrix-1"},
        )

    assert resp.status_code == 200
    headers = resp.headers.get_list("set-cookie")
    session_h = next(h for h in headers if h.startswith("ac_session=")).lower()
    csrf_h = next(h for h in headers if h.startswith("ac_csrf=")).lower()
    # 会话 cookie：HttpOnly + Secure + Strict + Path=/
    assert "httponly" in session_h and "secure" in session_h
    assert "samesite=strict" in session_h and "path=/" in session_h
    # CSRF cookie：非 HttpOnly（A14：SPA 可读），其余属性一致
    assert "httponly" not in csrf_h
    assert "secure" in csrf_h and "samesite=strict" in csrf_h and "path=/" in csrf_h


# ---------- 401 SESSION_EXPIRED 失效形态统一（逐字节一致，防探测）----------


async def test_session_expired_401_uniform_byte_identical(pg, flow_env):
    uid1 = await _seed_user(pg, "exp@example.com")
    async with owner_session(flow_env, uid1) as db:
        expired_token, _ = await create_session(db, user_id=uuid.UUID(uid1), device_label=_UA)
    uid2 = await _seed_user(pg, "revo@example.com")
    async with owner_session(flow_env, uid2) as db:
        revoked_token, _ = await create_session(db, user_id=uuid.UUID(uid2), device_label=_UA)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sessions SET expires_at = now() - interval '1 hour' WHERE token_hash = :h"
            ),
            {"h": hash_token(expired_token)},
        )
        await conn.execute(
            text("UPDATE sessions SET revoked_at = now() WHERE token_hash = :h"),
            {"h": hash_token(revoked_token)},
        )

    cases = [
        ("expired", {"ac_session": expired_token}),
        ("revoked", {"ac_session": revoked_token}),
        ("absent", {}),
    ]
    bodies = []
    for mode, cookies in cases:
        async with http_client("10.0.70.15", cookies=cookies) as client:
            resp = await client.post(_ROUTE_LOGOUT)
        assert resp.status_code == 401, mode
        assert resp.json() == _SESSION_EXPIRED_401, mode
        bodies.append(resp.content)
    assert len(set(bodies)) == 1  # 全部失效形态响应体逐字节一致（不泄漏失效模式）
