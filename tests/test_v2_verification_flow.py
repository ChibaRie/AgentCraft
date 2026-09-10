"""邮箱验证 confirm/resend 端到端测试（Task 10）。

契约出处：task-10-brief + Supplement §2/§7。

- confirm（公开端点，无会话无 CSRF，无限流——滥用面由令牌单次消费约束）：幂等
  begin 最先（同 key 重放优先于一切状态检查）→ admin 预检按 token_hash+purpose
  定位令牌行 → owner 事务（GUC=令牌行 user_id）内 FOR UPDATE 重校验 + pending→
  active 跃迁（已 active → 幂等成功仍消费令牌；suspended/deleting → 统一 400 且
  事务回滚分文不消费）→ 消费令牌 → 幂等 store。全部失效形态统一 400
  EMAIL_NOT_VERIFIED「验证链接无效或已过期」（响应体逐字节一致，防探测）。
- resend（认证端点）：真实会话 + CSRF 过 get_v2_auth 门序（Cookie + X-CSRF-Token）；
  限流 email_verify_resend（3/h，主体 [HMAC(user_id)]，enforce 自管事务独立提交）
  → 状态门（仅 pending，否则 409 VALIDATION_ERROR）→ owner 事务：旧未消费令牌
  全部作废 + 新令牌（72h）+ outbox 同事务。非幂等键控端点（A5 未列入）。
- HTTP 层：httpx.AsyncClient(ASGITransport(app, client=(ip, port))) 于测试自身循环
  驱动真实 app（T9 house pattern：TestClient 的 portal 跨循环与 asyncpg 连接池
  冲突；lifespan 不执行）。
- 认证客户端构造（T11-T14 可复用）：``authenticated_client``——superuser 播种用户
  + owner 事务 create_session（真实 RLS 面，会话/CSRF 明文仅此一次）→ AsyncClient
  预置 ac_session cookie（jar 随请求发送），POST 显式携带 X-CSRF-Token 明文。
- 种子/复核一律 superuser（pg.engine）：users/account_action_tokens/email_outbox
  均 owner-RLS，app role 无 GUC 盲区、admin role 仅 *_admin_read。
- 密钥注入：RATE_LIMIT_HMAC_KEY / EMAIL_OUTBOX_ENCRYPTION_KEY 每次现读 Settings，
  经 monkeypatch.setenv 注入一次性 b64url 材料（同 T9 flow_env，用例间互不污染）。
"""

import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from backend.main import app
from backend.v2.rate_limit import hmac_subject
from backend.v2.runtime import get_v2_runtime, owner_session
from backend.v2.security import generate_token, hash_token
from backend.v2.session_service import create_session
from tests.conftest import PgDb
from tests.test_v2_runtime import make_v2_runtime

_ROUTE_CONFIRM = "/api/v2/auth/email-verification/confirm"
_ROUTE_RESEND = "/api/v2/auth/email-verification/resend"
_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()  # 仅测试材料
_UA = "AgentCraft-FlowTest/1.0"

# 统一失败文案（契约钉死）：confirm 全部失效形态同码同文案同状态（防探测）
_UNIFORM_400 = {"error": {"code": "EMAIL_NOT_VERIFIED", "message": "验证链接无效或已过期"}}
_RESEND_409 = {"error": {"code": "VALIDATION_ERROR", "message": "当前账户状态无法重发验证邮件"}}
_CSRF_403 = {"error": {"code": "CSRF_INVALID", "message": "CSRF 校验失败"}}


@pytest.fixture
async def flow_env(pg: PgDb, monkeypatch):
    """app/admin 双 role runtime + FastAPI 依赖 override + 双密钥注入；yield runtime。"""
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("EMAIL_OUTBOX_ENCRYPTION_KEY", _KEY_MATERIAL)
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield rt
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.close()  # 同步 dispose 双引擎连接池（sync_engine.dispose）


def http_client(ip: str, cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
    """ASGITransport 驱动真实 app；client=(ip, port) 即 scope socket peer（client_ip 读取面）。"""
    transport = httpx.ASGITransport(app=app, client=(ip, 51000))
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"User-Agent": _UA},
        cookies=cookies or {},
    )


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


async def _seed_user(pg: PgDb, email: str, *, status: str = "pending") -> str:
    """播种用户行，返回 user id（asyncpg uuid → str）。owner-RLS：app role 无 GUC 盲区。"""
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


async def _seed_action_token(
    pg: PgDb,
    user_id: str,
    *,
    purpose: str = "email_verify",
    consumed: bool = False,
    expired: bool = False,
) -> str:
    """播种 account_action_tokens 行，返回明文 token（库存 sha256）。"""
    token = generate_token()
    now = datetime.now(timezone.utc)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO account_action_tokens (id, user_id, purpose, token_hash, "
                "expires_at, consumed_at) VALUES (gen_random_uuid(), :u, :p, :h, :exp, :c)"
            ),
            {
                "u": str(user_id),
                "p": purpose,
                "h": hash_token(token),
                "exp": now - timedelta(hours=1) if expired else now + timedelta(hours=72),
                "c": now if consumed else None,
            },
        )
    return token


async def _count(pg: PgDb, table: str, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {})
        ).scalar_one()


async def _one(pg: PgDb, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


async def _all(pg: PgDb, sql: str, params: dict | None = None) -> list[dict]:
    async with pg.engine.connect() as conn:
        rows = (await conn.execute(text(sql), params or {})).mappings().all()
        return [dict(r) for r in rows]


# ---------- 认证 HTTP 客户端（T11-T14 复用）----------


@asynccontextmanager
async def authenticated_client(
    pg: PgDb,
    rt,
    *,
    email: str,
    status: str = "pending",
    ip: str = "10.0.20.1",
    device: str = _UA,
):
    """构造带真实会话的认证 HTTP 客户端；yield (client, csrf 明文, user_id)。

    superuser 播种用户（绕 owner-RLS）→ owner 事务内 create_session（INSERT 满足
    sessions_app_insert WITH CHECK，会话/CSRF 明文仅此一次）→ AsyncClient 预置
    ac_session cookie（jar 随请求发送）。POST 请求须携带 ``X-CSRF-Token: csrf``。
    用法::

        async with authenticated_client(pg, rt, email="u@x.test") as (client, csrf, uid):
            resp = await client.post(_ROUTE_RESEND, headers={"X-CSRF-Token": csrf})
    """
    uid = await _seed_user(pg, email, status=status)
    async with owner_session(rt, str(uid)) as db:
        session_token, csrf_token = await create_session(db, user_id=uid, device_label=device)
    async with http_client(ip, cookies={"ac_session": session_token}) as client:
        yield client, csrf_token, uid


# ---------- confirm：pending→active 全链（HTTP 层）----------


async def test_confirm_full_chain_pending_to_active(pg, flow_env):
    uid = await _seed_user(pg, "verify-me@example.com")
    token = await _seed_action_token(pg, uid)
    async with http_client("10.0.1.1") as client:
        resp = await client.post(
            _ROUTE_CONFIRM, json={"verify_token": token}, headers={"Idempotency-Key": "confirm-1"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"data": {"status": "active"}}
    assert resp.headers.get_list("set-cookie") == []  # 公开端点不种 cookie

    # 用户 pending → active；令牌恰消费一次；无会话副作用
    u = await _one(pg, "SELECT status FROM users WHERE id = :u", {"u": uid})
    assert u["status"] == "active"
    at = await _one(
        pg, "SELECT consumed_at FROM account_action_tokens WHERE user_id = :u", {"u": uid}
    )
    assert at["consumed_at"] is not None
    assert await _count(pg, "sessions") == 0

    # 幂等记录恰一条：status_code=200 + 响应载荷（同事务落库）
    rec = await _one(pg, "SELECT status_code, response_json FROM idempotency_records")
    assert rec["status_code"] == 200
    assert rec["response_json"] == {"data": {"status": "active"}}


# ---------- confirm：失效形态统一 400 逐字节一致 + 零副作用 ----------


async def test_confirm_failure_modes_uniform_400_byte_identical(pg, flow_env):
    expired_uid = await _seed_user(pg, "cf-expired@example.com")
    expired_token = await _seed_action_token(pg, expired_uid, expired=True)
    wrong_purpose_uid = await _seed_user(pg, "cf-wpurpose@example.com")
    wrong_purpose_token = await _seed_action_token(pg, wrong_purpose_uid, purpose="password_reset")
    suspended_uid = await _seed_user(pg, "cf-suspended@example.com", status="suspended")
    suspended_token = await _seed_action_token(pg, suspended_uid)
    deleting_uid = await _seed_user(pg, "cf-deleting@example.com", status="deleting")
    deleting_token = await _seed_action_token(pg, deleting_uid)

    requests = [
        ("unknown_token", generate_token()),
        ("expired_72h", expired_token),
        ("wrong_purpose", wrong_purpose_token),
        ("suspended_user", suspended_token),
        ("deleting_user", deleting_token),
    ]
    bodies = []
    async with http_client("10.0.2.1") as client:
        for i, (mode, token) in enumerate(requests):
            resp = await client.post(
                _ROUTE_CONFIRM,
                json={"verify_token": token},
                headers={"Idempotency-Key": f"cf-uniform-{i}"},
            )
            assert resp.status_code == 400, mode
            assert resp.json() == _UNIFORM_400, mode
            bodies.append(resp.content)
    assert len(set(bodies)) == 1  # 全部失效形态响应体逐字节一致（防探测）

    # 失败路径零消费（事务回滚：suspended/deleting 令牌分文不消费）、状态不动
    rows = await _all(pg, "SELECT user_id, purpose, consumed_at FROM account_action_tokens")
    assert len(rows) == 4
    assert all(r["consumed_at"] is None for r in rows)
    statuses = {r["id"]: r["status"] for r in await _all(pg, "SELECT id, status FROM users")}
    assert statuses == {
        expired_uid: "pending",
        wrong_purpose_uid: "pending",
        suspended_uid: "suspended",
        deleting_uid: "deleting",
    }
    assert await _count(pg, "idempotency_records") == 0  # 失败路径不写幂等记录


async def test_confirm_consumed_token_new_key_uniform_400(pg, flow_env):
    uid = await _seed_user(pg, "once@example.com")
    token = await _seed_action_token(pg, uid)
    async with http_client("10.0.3.1") as client:
        first = await client.post(
            _ROUTE_CONFIRM, json={"verify_token": token}, headers={"Idempotency-Key": "k1"}
        )
        assert first.status_code == 200
        second = await client.post(
            _ROUTE_CONFIRM, json={"verify_token": token}, headers={"Idempotency-Key": "k2"}
        )

    assert second.status_code == 400
    assert second.json() == _UNIFORM_400
    assert await _count(pg, "idempotency_records") == 1  # 失败路径不新增幂等记录
    assert await _count(pg, "users", "status = 'active'") == 1  # 首次跃迁不受影响


# ---------- confirm：同 key 重放原样 200（状态无关）----------


async def test_confirm_replay_same_key_returns_original_200_state_independent(pg, flow_env):
    uid = await _seed_user(pg, "replay-cf@example.com")
    token = await _seed_action_token(pg, uid)
    async with http_client("10.0.4.1") as client:
        first = await client.post(
            _ROUTE_CONFIRM, json={"verify_token": token}, headers={"Idempotency-Key": "replay-cf"}
        )
        assert first.status_code == 200
        second = await client.post(
            _ROUTE_CONFIRM, json={"verify_token": token}, headers={"Idempotency-Key": "replay-cf"}
        )

    assert second.status_code == 200
    assert second.json() == first.json() == {"data": {"status": "active"}}
    assert await _count(pg, "idempotency_records") == 1  # 重放不新增
    assert await _count(pg, "users", "status = 'active'") == 1
    # 重放发生于令牌已消费之后：begin 命中先于一切状态检查（§7）


# ---------- confirm：已 active 用户持有效令牌 → 幂等成功且令牌被消费 ----------


async def test_confirm_already_active_user_consumes_token(pg, flow_env):
    uid = await _seed_user(pg, "again@example.com", status="active")
    token = await _seed_action_token(pg, uid)
    async with http_client("10.0.5.1") as client:
        resp = await client.post(
            _ROUTE_CONFIRM, json={"verify_token": token}, headers={"Idempotency-Key": "k-active"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"data": {"status": "active"}}
    at = await _one(
        pg, "SELECT consumed_at FROM account_action_tokens WHERE user_id = :u", {"u": uid}
    )
    assert at["consumed_at"] is not None  # 有效令牌即便重复点击也消费
    assert await _count(pg, "idempotency_records") == 1


# ---------- confirm：缺 Idempotency-Key / 超长 token（schema 边界）----------


async def test_confirm_missing_idempotency_key_rejected_400(pg, flow_env):
    uid = await _seed_user(pg, "nokey-cf@example.com")
    token = await _seed_action_token(pg, uid)
    async with http_client("10.0.6.1") as client:
        resp = await client.post(_ROUTE_CONFIRM, json={"verify_token": token})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    at = await _one(
        pg, "SELECT consumed_at FROM account_action_tokens WHERE user_id = :u", {"u": uid}
    )
    assert at["consumed_at"] is None
    assert await _count(pg, "idempotency_records") == 0


async def test_confirm_oversized_token_rejected_400_no_db_side_effects(pg, flow_env):
    """超长 verify_token（257 > 256）→ 400 零副作用；256 边界过门落入业务统一 400。"""
    uid = await _seed_user(pg, "big-tok@example.com")
    token = await _seed_action_token(pg, uid)
    async with http_client("10.0.7.1") as client:
        resp = await client.post(
            _ROUTE_CONFIRM,
            json={"verify_token": "t" * 257},
            headers={"Idempotency-Key": "big-tok"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await _count(pg, "idempotency_records") == 0  # schema 校验先于幂等与业务
        assert await _count(pg, "rate_limit_events") == 0

        # 边界钉死：256 字符（合法 token 43 字符的宽裕上限）通过 schema 门，进业务判 400
        edge = await client.post(
            _ROUTE_CONFIRM,
            json={"verify_token": "t" * 256},
            headers={"Idempotency-Key": "big-tok-edge"},
        )

    assert edge.status_code == 400
    assert edge.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"
    at = await _one(
        pg,
        "SELECT consumed_at FROM account_action_tokens WHERE token_hash = :h",
        {"h": hash_token(token)},
    )
    assert at["consumed_at"] is None  # 真实令牌未被误消费


# ---------- resend：pending 用户重发作废旧令牌并签发新令牌 ----------


async def test_resend_pending_user_invalidates_old_and_issues_new(pg, flow_env):
    async with authenticated_client(pg, flow_env, email="resend@example.com", ip="10.0.8.1") as (
        client,
        csrf,
        uid,
    ):
        old1 = await _seed_action_token(pg, uid)  # 旧未消费 → 应被作废
        old2 = await _seed_action_token(pg, uid, consumed=True)  # 已消费 → 保持
        resp = await client.post(_ROUTE_RESEND, headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 200
    assert resp.json() == {"data": {"sent": True}}
    assert "token" not in resp.text  # 令牌仅经邮件投递，不入响应体

    rows = await _all(
        pg,
        "SELECT token_hash, consumed_at, expires_at, now() AS db_now "
        "FROM account_action_tokens WHERE user_id = :u AND purpose = 'email_verify'",
        {"u": uid},
    )
    assert len(rows) == 3  # 2 旧 + 1 新
    by_hash = {r["token_hash"]: r for r in rows}
    assert by_hash[hash_token(old1)]["consumed_at"] is not None  # 旧未消费被作废
    fresh = [r for r in rows if r["consumed_at"] is None]
    assert len(fresh) == 1
    assert fresh[0]["token_hash"] not in (hash_token(old1), hash_token(old2))
    diff = fresh[0]["expires_at"] - fresh[0]["db_now"]
    assert timedelta(hours=71, minutes=59) < diff <= timedelta(hours=72)  # 72h 有效期

    # outbox 同事务落行（transactional outbox）
    ob = await _one(
        pg, "SELECT user_id, purpose, state FROM email_outbox WHERE user_id = :u", {"u": uid}
    )
    assert ob["user_id"] == uid and ob["purpose"] == "email_verify" and ob["state"] == "pending"

    # 限流事件恰一行（单主体 [HMAC(user_id)]）
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'email_verify_resend' AND subject_hash = :h",
        {"h": hmac_subject("user", str(uid))},
    )
    assert n == 1


# ---------- resend：第 4 次 429 + Retry-After ----------


async def test_resend_fourth_time_rate_limited_429_with_retry_after(pg, flow_env):
    async with authenticated_client(pg, flow_env, email="rl-resend@example.com", ip="10.0.9.1") as (
        client,
        csrf,
        uid,
    ):
        for _ in range(3):
            resp = await client.post(_ROUTE_RESEND, headers={"X-CSRF-Token": csrf})
            assert resp.status_code == 200
        fourth = await client.post(_ROUTE_RESEND, headers={"X-CSRF-Token": csrf})

    assert fourth.status_code == 429
    assert fourth.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert 1 <= int(fourth.headers["Retry-After"]) <= 3600
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'email_verify_resend' AND subject_hash = :h",
        {"h": hmac_subject("user", str(uid))},
    )
    assert n == 3  # 恰 3 行事件（拒绝路径不写）


# ---------- resend：active 用户 409 VALIDATION_ERROR ----------


async def test_resend_active_user_409_validation_error(pg, flow_env):
    async with authenticated_client(
        pg, flow_env, email="active-resend@example.com", status="active", ip="10.0.10.1"
    ) as (client, csrf, uid):
        resp = await client.post(_ROUTE_RESEND, headers={"X-CSRF-Token": csrf})

    assert resp.status_code == 409
    assert resp.json() == _RESEND_409
    assert await _count(pg, "account_action_tokens", "user_id = :u", {"u": uid}) == 0
    assert await _count(pg, "email_outbox", "user_id = :u", {"u": uid}) == 0  # 零业务副作用


# ---------- resend：缺 X-CSRF-Token → 403 CSRF_INVALID（get_v2_auth 门）----------


async def test_resend_without_csrf_rejected_403(pg, flow_env):
    async with authenticated_client(
        pg, flow_env, email="csrf-resend@example.com", ip="10.0.11.1"
    ) as (client, _csrf, uid):
        resp = await client.post(_ROUTE_RESEND)  # 无 X-CSRF-Token

    assert resp.status_code == 403
    assert resp.json() == _CSRF_403
    assert await _count(pg, "account_action_tokens", "user_id = :u", {"u": uid}) == 0
    assert await _count(pg, "email_outbox", "user_id = :u", {"u": uid}) == 0
