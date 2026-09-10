"""会话服务测试（Task 7）：不透明令牌往返、滑动刷新、软撤销、设备列表与 get_v2_auth 门序。

契约出处：task-7-brief（interfaces-first）+ Database Design §3 sessions owner-RLS。

- 真实 RLS 面：业务写全部经 owner_session（GUC 事务内，INSERT 命中
  sessions_app_insert WITH CHECK）；种子与复核经 superuser（pg.engine——
  users/sessions 均 owner-RLS，app/admin role 无 INSERT policy）。
- pre-auth 解析必须走 admin role（chicken-and-egg：按 token 找会话发生在
  知道 user 之前）——RLS 面用例断言 admin 可达 / app role 无 GUC 盲区。
- get_v2_auth 测试方式（brief 允许三选一，此处选「手工构造输入直调依赖」）：
  scope 内联 method / Cookie / X-CSRF-Token 构造 Starlette Request，显式传入
  runtime 与 admin_db。不走临时 APIRouter + TestClient：pytest-asyncio 每用例
  独立事件循环，TestClient 的 portal 独立循环与 asyncpg 连接池绑定冲突
  （连接跨循环复用即 RuntimeError），直调同时让门序矩阵逐门精确断言。
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import text
from starlette.requests import Request as StarletteRequest

from backend.v2 import session_service
from backend.v2.runtime import owner_session
from backend.v2.security import hash_token
from tests.conftest import PgDb
from tests.test_v2_runtime import make_v2_runtime

# 统一失败文案（契约钉死）：任何会话失效形态都长同一张脸，不泄漏失效模式
_EXPIRED_DETAIL = {"code": "SESSION_EXPIRED", "message": "登录状态已失效，请重新登录"}
_CSRF_DETAIL = {"code": "CSRF_INVALID", "message": "CSRF 校验失败"}


@pytest.fixture
async def v2_runtime(pg: PgDb):
    """app/admin 双 role 运行时（复用 test_v2_runtime 构建器；不挂 FastAPI override）。"""
    rt = make_v2_runtime(pg)
    yield rt
    rt.close()


# ---------- 种子与复核助手（superuser 绕 RLS；role 会话无 INSERT 权限）----------


async def _seed_user(pg: PgDb, email: str = "sess@x.test", status: str = "active") -> _uuid.UUID:
    async with pg.engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), :e, 'h', 'user', :s) RETURNING id"
                ),
                {"e": email, "s": status},
            )
        ).scalar_one()


async def _new_session(
    rt, uid: _uuid.UUID, device: str = "Chrome on Windows", mfa: bool = False
) -> tuple[str, str]:
    """owner 事务内建会话（GUC 先行，INSERT 满足 sessions_app_insert WITH CHECK）。"""
    async with owner_session(rt, str(uid)) as db:
        return await session_service.create_session(
            db, user_id=uid, device_label=device, mfa_verified=mfa
        )


async def _row_by_hash(pg: PgDb, token_hash: str):
    """superuser 按 token_hash 复核会话行（绕开被测会话状态）。"""
    async with pg.engine.begin() as conn:
        return (
            (
                await conn.execute(
                    text(
                        "SELECT id, created_at, expires_at, revoked_at, device_label "
                        "FROM sessions WHERE token_hash = :h"
                    ),
                    {"h": token_hash},
                )
            )
            .mappings()
            .one()
        )


async def _db_now(pg: PgDb) -> datetime:
    async with pg.engine.connect() as conn:
        return (await conn.execute(text("SELECT now()"))).scalar_one()


# ---------- create → admin resolve 往返 ----------


async def test_create_returns_plaintext_once_and_admin_resolve_roundtrip(pg, v2_runtime):
    uid = await _seed_user(pg)
    token, csrf = await _new_session(v2_runtime, uid)

    async with v2_runtime.admin_factory() as admin_db:
        pair = await session_service.resolve_session(admin_db, token)
    assert pair is not None
    session, user = pair
    # 库存 sha256 哈希、明文仅此一次
    assert session.token_hash == hash_token(token)
    assert session.token_hash != token
    assert session.csrf_hash == hash_token(csrf)
    assert user.id == uid
    assert user.status == "active"
    assert session.revoked_at is None
    assert session.device_label == "Chrome on Windows"
    assert session.mfa_verified_at is None  # 默认未经 MFA
    delta = session.expires_at - session.created_at
    assert abs(delta - timedelta(days=7)) < timedelta(minutes=1)  # expires_at = now()+7d


async def test_create_with_mfa_stamps_verified_at(pg, v2_runtime):
    uid = await _seed_user(pg)
    token, _ = await _new_session(v2_runtime, uid, mfa=True)
    async with v2_runtime.admin_factory() as admin_db:
        session, _ = await session_service.resolve_session(admin_db, token)
    assert session.mfa_verified_at is not None
    assert abs(datetime.now(timezone.utc) - session.mfa_verified_at) < timedelta(minutes=1)


# ---------- resolve 不可解析矩阵（过期 / 已撤销 / deleted 用户）----------


async def test_resolve_rejects_expired_session(pg, v2_runtime):
    uid = await _seed_user(pg)
    token, _ = await _new_session(v2_runtime, uid)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text("UPDATE sessions SET expires_at = now() - interval '1 hour' WHERE user_id = :u"),
            {"u": uid},
        )
    async with v2_runtime.admin_factory() as admin_db:
        assert await session_service.resolve_session(admin_db, token) is None


async def test_resolve_rejects_revoked_session(pg, v2_runtime):
    uid = await _seed_user(pg)
    token, _ = await _new_session(v2_runtime, uid)
    sid = (await _row_by_hash(pg, hash_token(token)))["id"]
    async with owner_session(v2_runtime, str(uid)) as db:
        await session_service.revoke(db, sid)
    async with v2_runtime.admin_factory() as admin_db:
        assert await session_service.resolve_session(admin_db, token) is None


async def test_resolve_rejects_deleted_user(pg, v2_runtime):
    uid = await _seed_user(pg)
    token, _ = await _new_session(v2_runtime, uid)
    async with pg.engine.begin() as conn:
        await conn.execute(text("UPDATE users SET status = 'deleted' WHERE id = :i"), {"i": uid})
    async with v2_runtime.admin_factory() as admin_db:
        assert await session_service.resolve_session(admin_db, token) is None


# ---------- 滑动刷新（单条件语句；命中阈值滑动，绝对上限 least 收口）----------


async def test_refresh_within_threshold_extends_to_full_ttl(pg, v2_runtime):
    uid = await _seed_user(pg)
    token, _ = await _new_session(v2_runtime, uid)
    before = await _row_by_hash(pg, hash_token(token))
    async with pg.engine.begin() as conn:  # 拨入阈值内：剩 3 天 < 6 天
        await conn.execute(
            text("UPDATE sessions SET expires_at = now() + interval '3 days' WHERE id = :i"),
            {"i": before["id"]},
        )
    async with owner_session(v2_runtime, str(uid)) as db:
        await session_service.refresh_if_needed(db, before["id"])
    after = await _row_by_hash(pg, hash_token(token))
    assert after["expires_at"] > before["expires_at"]  # 确实向前滑动
    left = after["expires_at"] - await _db_now(pg)
    assert timedelta(days=7) - timedelta(seconds=5) < left <= timedelta(days=7)  # 满额 7d


async def test_refresh_outside_threshold_is_noop(pg, v2_runtime):
    uid = await _seed_user(pg)
    token, _ = await _new_session(v2_runtime, uid)
    before = await _row_by_hash(pg, hash_token(token))
    async with owner_session(v2_runtime, str(uid)) as db:
        await session_service.refresh_if_needed(db, before["id"])
    after = await _row_by_hash(pg, hash_token(token))
    assert after["expires_at"] == before["expires_at"]  # 阈值外零写入


async def test_refresh_caps_at_created_at_plus_absolute(pg, v2_runtime):
    """老会话（created 25 天前、剩 3 天）：刷新被 least 钉在 created_at+30d。"""
    uid = await _seed_user(pg)
    plaintext = "capped-session-token"
    token_hash = hash_token(plaintext)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO sessions (id, token_hash, user_id, csrf_hash, expires_at, "
                "created_at) VALUES (gen_random_uuid(), :t, :u, :c, "
                "now() + interval '3 days', now() - interval '25 days')"
            ),
            {"t": token_hash, "u": uid, "c": "f" * 64},
        )
    sid = (await _row_by_hash(pg, token_hash))["id"]
    async with owner_session(v2_runtime, str(uid)) as db:
        await session_service.refresh_if_needed(db, sid)
    row = await _row_by_hash(pg, token_hash)
    assert row["expires_at"] - row["created_at"] == timedelta(days=30)  # 绝对上限生效


# ---------- 软撤销（行保留；rowcount 只计本次真正翻 revoked_at 的行）----------


async def test_revoke_soft_revokes_once_and_is_idempotent(pg, v2_runtime):
    uid = await _seed_user(pg)
    t1, _ = await _new_session(v2_runtime, uid)
    t2, _ = await _new_session(v2_runtime, uid)
    s1 = (await _row_by_hash(pg, hash_token(t1)))["id"]
    async with owner_session(v2_runtime, str(uid)) as db:
        assert await session_service.revoke(db, s1) == 1
        assert await session_service.revoke(db, s1) == 0  # 幂等：已撤销行不再计数
    assert (await _row_by_hash(pg, hash_token(t1)))["revoked_at"] is not None
    async with v2_runtime.admin_factory() as admin_db:  # 软撤销：行仍在，仅解析失效
        assert await session_service.resolve_session(admin_db, t1) is None
        assert await session_service.resolve_session(admin_db, t2) is not None


async def test_revoke_all_counts_only_unrevoked(pg, v2_runtime):
    uid = await _seed_user(pg)
    tokens = []
    for _ in range(3):
        tok, _ = await _new_session(v2_runtime, uid)
        tokens.append(tok)
    s1 = (await _row_by_hash(pg, hash_token(tokens[0])))["id"]
    async with owner_session(v2_runtime, str(uid)) as db:
        await session_service.revoke(db, s1)
    async with owner_session(v2_runtime, str(uid)) as db:
        assert await session_service.revoke_all(db, uid) == 2  # 只算未撤销行
    async with owner_session(v2_runtime, str(uid)) as db:
        assert await session_service.revoke_all(db, uid) == 0  # 幂等


# ---------- list_sessions：字段契约、current 标记、owner 面隔离、device_label 可空 ----------


async def test_list_sessions_marks_current_owner_scope_and_null_device(pg, v2_runtime):
    uid = await _seed_user(pg)
    t1, _ = await _new_session(v2_runtime, uid, device="Chrome on Windows")
    t2, _ = await _new_session(v2_runtime, uid, device="Safari on iPhone")
    async with pg.engine.begin() as conn:  # device_label NULL 面：superuser 直种
        null_id = (
            await conn.execute(
                text(
                    "INSERT INTO sessions (id, token_hash, user_id, csrf_hash, expires_at) "
                    "VALUES (gen_random_uuid(), :t, :u, :c, now() + interval '7 days') "
                    "RETURNING id"
                ),
                {"t": "a" * 64, "u": uid, "c": "b" * 64},
            )
        ).scalar_one()
    other = await _seed_user(pg, "other-sess@x.test")
    await _new_session(v2_runtime, other, device="Other user device")  # 不得出现在列表

    current_id = (await _row_by_hash(pg, hash_token(t2)))["id"]
    async with owner_session(v2_runtime, str(uid)) as db:
        items = await session_service.list_sessions(db, current_id)

    assert len(items) == 3  # owner 面只看自己的会话
    ids = {(await _row_by_hash(pg, hash_token(t1)))["id"], current_id, null_id}
    assert {i["id"] for i in items} == ids
    for item in items:
        assert set(item) == {"id", "device_label", "created_at", "expires_at", "current"}
    by_id = {i["id"]: i for i in items}
    assert by_id[current_id]["current"] is True
    assert all(i["current"] is False for k, i in by_id.items() if k != current_id)
    assert {i["device_label"] for i in items} == {"Chrome on Windows", "Safari on iPhone", None}


# ---------- RLS 面：admin 解析可达（pre-auth 鸡生蛋），app role 无 GUC 盲区 ----------


async def test_session_rls_face_admin_reachable_app_blind(pg, v2_runtime):
    uid = await _seed_user(pg)
    token, _ = await _new_session(v2_runtime, uid)
    async with v2_runtime.admin_factory() as admin_db:  # admin 面：USING(true) policy
        assert await session_service.resolve_session(admin_db, token) is not None
    async with v2_runtime.app_factory() as app_db:  # app 面无 GUC：0 行盲区
        n = (await app_db.execute(text("SELECT count(*) FROM sessions"))).scalar_one()
        assert n == 0


# ---------- Cookie 助手（检查 fastapi.Response 原始 set-cookie 头，不起服务器）----------


def _set_cookie_jar(response: Response) -> SimpleCookie:
    jar = SimpleCookie()
    for key, value in response.raw_headers:
        if key == b"set-cookie":
            jar.load(value.decode("latin-1"))
    return jar


def test_set_session_cookie_attributes():
    resp = Response()
    session_service.set_session_cookie(resp, "sess-token")
    jar = _set_cookie_jar(resp)
    assert set(jar) == {"ac_session"}  # 不顺手种 CSRF
    morsel = jar["ac_session"]
    assert morsel.value == "sess-token"
    assert morsel["httponly"] is True
    assert morsel["secure"] is True  # 测试环境 SESSION_COOKIE_SECURE 默认 True
    assert morsel["samesite"] == "strict"
    assert morsel["path"] == "/"
    assert morsel["max-age"] == str(int(session_service.SESSION_ABSOLUTE.total_seconds()))


def test_set_csrf_cookie_is_spa_readable():
    resp = Response()
    session_service.set_csrf_cookie(resp, "csrf-token")
    jar = _set_cookie_jar(resp)
    assert set(jar) == {"ac_csrf"}
    morsel = jar["ac_csrf"]
    assert morsel.value == "csrf-token"
    assert not morsel["httponly"]  # A14 交付信道：SPA 必须可读（非 HttpOnly）
    assert morsel["secure"] is True
    assert morsel["samesite"] == "strict"
    assert morsel["path"] == "/"
    assert morsel["max-age"] == str(int(session_service.SESSION_ABSOLUTE.total_seconds()))


def test_clear_session_cookie_clears_both():
    resp = Response()
    session_service.set_session_cookie(resp, "sess-token")
    session_service.set_csrf_cookie(resp, "csrf-token")
    session_service.clear_session_cookie(resp)
    jar = _set_cookie_jar(resp)
    assert set(jar) == {"ac_session", "ac_csrf"}  # 双 cookie 一并清除
    for name in ("ac_session", "ac_csrf"):
        assert jar[name].value == ""
        assert jar[name]["max-age"] == "0"


# ---------- get_v2_auth 门序矩阵（直调依赖；见模块 docstring 的方式裁决）----------


def _request(
    method: str = "GET", token: str | None = None, csrf: str | None = None
) -> StarletteRequest:
    headers = []
    if token is not None:
        headers.append((b"cookie", f"{session_service.COOKIE_NAME}={token}".encode()))
    if csrf is not None:
        headers.append((b"x-csrf-token", csrf.encode()))
    scope = {
        "type": "http",
        "method": method,
        "path": "/",
        "headers": headers,
        "query_string": b"",
    }
    return StarletteRequest(scope)


async def _auth(request: StarletteRequest, rt, admin_db):
    return await session_service.get_v2_auth(request, runtime=rt, admin_db=admin_db)


async def _prepared(pg: PgDb, rt, status: str = "active") -> tuple[_uuid.UUID, str, str]:
    uid = await _seed_user(pg, status=status)
    token, csrf = await _new_session(rt, uid)
    return uid, token, csrf


@pytest.mark.parametrize(
    "scenario",
    ["no_cookie", "unknown_token", "expired", "revoked", "deleted_user"],
)
async def test_auth_failures_are_uniform_401(pg, v2_runtime, scenario):
    uid, token, _ = await _prepared(pg, v2_runtime)
    if scenario == "expired":
        async with pg.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE sessions SET expires_at = now() - interval '1 min' WHERE user_id = :u"
                ),
                {"u": uid},
            )
    elif scenario == "revoked":
        sid = (await _row_by_hash(pg, hash_token(token)))["id"]
        async with owner_session(v2_runtime, str(uid)) as db:
            await session_service.revoke(db, sid)
    elif scenario == "deleted_user":
        async with pg.engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET status = 'deleted' WHERE id = :i"), {"i": uid}
            )
    cookie_token = None
    if scenario not in ("no_cookie",):
        cookie_token = "unknown-token" if scenario == "unknown_token" else token
    async with v2_runtime.admin_factory() as admin_db:
        with pytest.raises(HTTPException) as excinfo:
            await _auth(_request(token=cookie_token), v2_runtime, admin_db)
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == _EXPIRED_DETAIL  # 统一文案：无效/过期/撤销/deleted 同脸


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_auth_write_methods_require_csrf_header(pg, v2_runtime, method):
    _, token, _ = await _prepared(pg, v2_runtime)
    async with v2_runtime.admin_factory() as admin_db:
        with pytest.raises(HTTPException) as missing:
            await _auth(_request(method, token=token), v2_runtime, admin_db)
        assert missing.value.status_code == 403
        assert missing.value.detail == _CSRF_DETAIL
        with pytest.raises(HTTPException) as wrong:
            await _auth(_request(method, token=token, csrf="forged-csrf"), v2_runtime, admin_db)
        assert wrong.value.status_code == 403
        assert wrong.value.detail == _CSRF_DETAIL


async def test_auth_valid_csrf_returns_context(pg, v2_runtime):
    uid, token, csrf = await _prepared(pg, v2_runtime)
    async with v2_runtime.admin_factory() as admin_db:
        ctx = await _auth(_request("POST", token=token, csrf=csrf), v2_runtime, admin_db)
    assert isinstance(ctx, session_service.V2AuthContext)
    assert ctx.user.id == uid
    assert ctx.session.token_hash == hash_token(token)
    assert ctx.session.csrf_hash == hash_token(csrf)


async def test_auth_get_passes_without_csrf(pg, v2_runtime):
    uid, token, _ = await _prepared(pg, v2_runtime)
    async with v2_runtime.admin_factory() as admin_db:
        ctx = await _auth(_request("GET", token=token), v2_runtime, admin_db)
    assert ctx.user.id == uid


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        ("pending", None),  # 放行
        ("active", None),
        ("suspended", "ACCOUNT_SUSPENDED"),
        ("deleting", "ACCOUNT_DELETING"),
    ],
)
async def test_auth_status_gate_matrix(pg, v2_runtime, status, expected_code):
    uid, token, _ = await _prepared(pg, v2_runtime, status=status)
    async with v2_runtime.admin_factory() as admin_db:
        if expected_code is None:
            ctx = await _auth(_request("GET", token=token), v2_runtime, admin_db)
            assert ctx.user.id == uid  # pending/active 放行
        else:
            with pytest.raises(HTTPException) as excinfo:
                await _auth(_request("GET", token=token), v2_runtime, admin_db)
            assert excinfo.value.status_code == 403
            assert excinfo.value.detail["code"] == expected_code


async def test_auth_triggers_sliding_refresh_within_threshold(pg, v2_runtime):
    uid, token, _ = await _prepared(pg, v2_runtime)
    async with pg.engine.begin() as conn:  # 拨入阈值内：剩 3 天
        await conn.execute(
            text("UPDATE sessions SET expires_at = now() + interval '3 days' WHERE user_id = :u"),
            {"u": uid},
        )
    async with v2_runtime.admin_factory() as admin_db:
        await _auth(_request("GET", token=token), v2_runtime, admin_db)
    row = await _row_by_hash(pg, hash_token(token))
    left = row["expires_at"] - await _db_now(pg)  # 刷新满额 7d
    assert timedelta(days=7) - timedelta(seconds=5) < left <= timedelta(days=7)


async def test_auth_refresh_respects_absolute_cap(pg, v2_runtime):
    uid = await _seed_user(pg)
    plaintext = "seeded-token-for-cap"
    async with pg.engine.begin() as conn:  # 老会话：created 25 天前、剩 3 天（阈值内）
        await conn.execute(
            text(
                "INSERT INTO sessions (id, token_hash, user_id, csrf_hash, expires_at, "
                "created_at) VALUES (gen_random_uuid(), :t, :u, :c, "
                "now() + interval '3 days', now() - interval '25 days')"
            ),
            {"t": hash_token(plaintext), "u": uid, "c": "f" * 64},
        )
    async with v2_runtime.admin_factory() as admin_db:
        await _auth(_request("GET", token=plaintext), v2_runtime, admin_db)
    row = await _row_by_hash(pg, hash_token(plaintext))
    assert row["expires_at"] - row["created_at"] == timedelta(days=30)  # 不越过 created+30d


async def test_auth_no_refresh_outside_threshold(pg, v2_runtime):
    uid, token, _ = await _prepared(pg, v2_runtime)
    before = await _row_by_hash(pg, hash_token(token))
    async with v2_runtime.admin_factory() as admin_db:
        await _auth(_request("GET", token=token), v2_runtime, admin_db)
    after = await _row_by_hash(pg, hash_token(token))
    assert after["expires_at"] == before["expires_at"]  # 阈值外不产生写开销
