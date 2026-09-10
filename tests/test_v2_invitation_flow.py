"""邀请接受端到端测试（Task 9）：并发安全消费、幂等重放、限流与防枚举统一 409。

契约出处：task-9-brief + Supplement §2/§7 + PRD §6（并发双消费恰一 user 行）。

- HTTP 层（成功路径/重放/限流/缺头）：经 httpx.AsyncClient(ASGITransport) 驱动真实
  app（依赖 override 注入测试 runtime）。方式裁决（承接 T7 教训）：TestClient 的
  blocking portal 是独立循环线程，runtime 的 asyncpg 连接池绑定测试循环，跨循环
  即 RuntimeError；ASGITransport 让 app 跑在测试自身循环里——引擎/种子/复核/请求
  同循环零冲突，且 lifespan 不执行（无 outbox 后台任务等副作用）。
- IP 变异：ASGITransport 构造参数 ``client=(ip, port)`` 直接写入 ASGI scope 的
  client（即 runtime.client_ip 读取的 socket peer 面），每用例独立 IP——pg 夹具
  每用例克隆独立库本就无跨用例污染，独立 IP 是用例内隔离与意图显式化。
- 种子/复核一律 superuser（pg.engine）：invitations 无 RLS 但 app role 无 INSERT；
  users/account_action_tokens/email_outbox 为 owner-RLS。
- 密钥注入：RATE_LIMIT_HMAC_KEY / EMAIL_OUTBOX_ENCRYPTION_KEY 每次现读
  Settings，经 monkeypatch.setenv 注入一次性 b64url 材料（同 test_v2_rate_limit /
  test_v2_outbox 模式，用例间互不污染）。
"""

import asyncio
import base64
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

import httpx
import pytest
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.main import app
from backend.v2 import invitation_service
from backend.v2.idempotency import request_hash
from backend.v2.ids import uuid7
from backend.v2.rate_limit import hmac_subject
from backend.v2.runtime import get_v2_runtime
from backend.v2.security import generate_token, hash_token, verify_password
from tests.conftest import PgDb
from tests.test_v2_runtime import make_v2_runtime

_ROUTE = "/api/v2/auth/invitations/accept"
_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()  # 仅测试材料
_UA = "AgentCraft-FlowTest/1.0"


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


def http_client(ip: str) -> httpx.AsyncClient:
    """ASGITransport 驱动真实 app；client=(ip, port) 即 scope socket peer（client_ip 读取面）。"""
    transport = httpx.ASGITransport(app=app, client=(ip, 51000))
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers={"User-Agent": _UA}
    )


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


async def _seed_invitation(
    pg: PgDb,
    email: str,
    *,
    consumed: bool = False,
    revoked: bool = False,
    expired: bool = False,
) -> tuple[str, _uuid.UUID]:
    """播种邀请行，返回 (明文 token, invitation id)。app role 无 invitations INSERT。"""
    token = generate_token()
    iid = uuid7()
    now = datetime.now(timezone.utc)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO invitations (id, token_hash, email, expires_at, consumed_at, "
                "revoked_at) VALUES (:i, :t, :e, :exp, :c, :r)"
            ),
            {
                "i": iid,
                "t": hash_token(token),
                "e": email,
                "exp": now - timedelta(hours=1) if expired else now + timedelta(days=7),
                "c": now if consumed else None,
                "r": now if revoked else None,
            },
        )
    return token, iid


async def _count(pg: PgDb, table: str, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {})
        ).scalar_one()


async def _one(pg: PgDb, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


def _accept_body(token: str, email: str, password: str) -> dict:
    return {"invitation_token": token, "email": email, "password": password}


async def _accept(
    client: httpx.AsyncClient,
    token: str,
    *,
    key: str,
    email: str = "invitee@example.com",
    password: str = "s3cret-pw-123",
    with_key: bool = True,
) -> httpx.Response:
    headers = {"Idempotency-Key": key} if with_key else {}
    return await client.post(_ROUTE, json=_accept_body(token, email, password), headers=headers)


def _set_cookies(resp: httpx.Response) -> SimpleCookie:
    """解析响应全部 Set-Cookie 头（不经 cookie jar 策略，Secure 属性不干扰断言）。"""
    jar = SimpleCookie()
    for raw in resp.headers.get_list("set-cookie"):
        jar.load(raw)
    return jar


# ---------- 成功路径全链（HTTP 层）----------


async def test_accept_success_creates_pending_user_session_outbox(pg, flow_env):
    ip = "10.0.1.1"
    token, iid = await _seed_invitation(pg, "invitee@example.com")
    async with http_client(ip) as client:
        resp = await _accept(
            client, token, key="k-success", email="Invitee@Example.com", password="s3cret-pw-123"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"data"}
    data = body["data"]
    assert set(data) == {"user_id", "email", "status", "csrf_token"}
    assert data["email"] == "invitee@example.com"  # email-validator 规范化为小写
    assert data["status"] == "pending"
    uid = _uuid.UUID(data["user_id"])

    # users：恰一行 pending/user，Argon2id 可验证
    assert await _count(pg, "users") == 1
    u = await _one(pg, "SELECT id, email, password_hash, role, status FROM users")
    assert u["id"] == uid
    assert u["email"] == "invitee@example.com"
    assert u["role"] == "user" and u["status"] == "pending"
    assert verify_password("s3cret-pw-123", u["password_hash"])

    # 邀请已消费（且未撤销）
    inv = await _one(
        pg, "SELECT consumed_at, revoked_at FROM invitations WHERE id = :i", {"i": iid}
    )
    assert inv["consumed_at"] is not None and inv["revoked_at"] is None

    # 会话：库存哈希与 cookie 明文互证；设备摘要来自 UA
    assert await _count(pg, "sessions") == 1
    s = await _one(pg, "SELECT token_hash, device_label, revoked_at, user_id FROM sessions")
    assert s["user_id"] == uid and s["revoked_at"] is None
    assert s["device_label"] == _UA

    # 双 cookie：ac_session HttpOnly；ac_csrf 非 HttpOnly 且与响应体一致
    jar = _set_cookies(resp)
    assert set(jar) == {"ac_session", "ac_csrf"}
    assert jar["ac_session"]["httponly"]
    assert not jar["ac_csrf"]["httponly"]
    assert jar["ac_csrf"].value == data["csrf_token"]
    assert s["token_hash"] == hash_token(jar["ac_session"].value)

    # 验证邮件 outbox 行 + email_verify action token（与业务同事务落库）
    assert await _count(pg, "email_outbox") == 1
    ob = await _one(pg, "SELECT user_id, purpose, state FROM email_outbox")
    assert ob["user_id"] == uid and ob["purpose"] == "email_verify" and ob["state"] == "pending"
    assert await _count(pg, "account_action_tokens") == 1
    at = await _one(
        pg, "SELECT purpose, consumed_at, expires_at, now() AS db_now FROM account_action_tokens"
    )
    assert at["purpose"] == "email_verify" and at["consumed_at"] is None
    assert timedelta(hours=71, minutes=59) < at["expires_at"] - at["db_now"] <= timedelta(hours=72)

    # 限流事件：[HMAC(ip)] 单主体一行
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'invitation_accept' AND subject_hash = :h",
        {"h": hmac_subject("ip", ip)},
    )
    assert n == 1


# ---------- 四失效形态统一 409 且逐字节一致；email 不符仅审计留痕 ----------


async def test_failure_modes_uniform_409_byte_identical_and_mismatch_audited(pg, flow_env):
    seeds = {
        "expired": await _seed_invitation(pg, "a1@example.com", expired=True),
        "consumed": await _seed_invitation(pg, "a2@example.com", consumed=True),
        "revoked": await _seed_invitation(pg, "a3@example.com", revoked=True),
        "email_mismatch": await _seed_invitation(pg, "a4@example.com"),
    }
    requests = [
        ("expired", seeds["expired"][0], "a1@example.com"),
        ("consumed", seeds["consumed"][0], "a2@example.com"),
        ("revoked", seeds["revoked"][0], "a3@example.com"),
        ("email_mismatch", seeds["email_mismatch"][0], "someone-else@example.com"),
        ("unknown_token", generate_token(), "a5@example.com"),
    ]
    bodies = []
    async with http_client("10.0.2.1") as client:
        for i, (mode, token, email) in enumerate(requests):
            resp = await _accept(client, token, key=f"idem-{i}", email=email)
            assert resp.status_code == 409, mode
            assert resp.json() == {
                "error": {"code": "INVITATION_INVALID", "message": "邀请无效或已过期"}
            }
            bodies.append(resp.content)
    assert len(set(bodies)) == 1  # 全部失效形态响应体逐字节一致（防探测）

    # 无任何 user 落建；失败路径不写幂等记录
    assert await _count(pg, "users") == 0
    assert await _count(pg, "idempotency_records") == 0

    # 仅 email 不符（邀请其余有效）写一条审计：admin 会话独立事务，独立于业务回滚存续
    audits = await _one(
        pg,
        "SELECT action, target_type, target_id, reason, actor_id FROM audit_logs",
    )
    assert audits["action"] == "invitation.email_mismatch"
    assert audits["target_type"] == "invitation"
    assert audits["target_id"] == seeds["email_mismatch"][1]
    assert audits["reason"] == "accept email mismatch"
    assert audits["actor_id"] is None
    assert await _count(pg, "audit_logs") == 1

    # 被审计的邀请保持 open（审计而非消费）
    inv = await _one(
        pg, "SELECT consumed_at FROM invitations WHERE id = :i", {"i": seeds["email_mismatch"][1]}
    )
    assert inv["consumed_at"] is None


# ---------- 并发双消费：恰一 200 一 409，users 恰 1 行（PRD §6）----------


async def test_concurrent_double_accept_exactly_one_winner(pg, flow_env):
    rt = flow_env
    token, _ = await _seed_invitation(pg, "race@example.com")
    req_hash = request_hash(
        {"invitation_token": token, "email": "race@example.com", "password": "pw-race"}
    )

    async def attempt(key: str):
        try:
            outcome = await invitation_service.accept_invitation(
                rt,
                invitation_token=token,
                email="race@example.com",
                password="pw-race",
                idem_key=key,
                idem_hash=req_hash,
                ip="10.0.3.1",
                device_label="race-runner",
            )
        except AgentCraftError as exc:
            return ("error", exc)
        return ("ok", outcome)

    first, second = await asyncio.gather(attempt("race-key-1"), attempt("race-key-2"))
    assert sorted([first[0], second[0]]) == ["error", "ok"]
    winner, loser = (first, second) if first[0] == "ok" else (second, first)
    assert winner[1].body["data"]["status"] == "pending"
    assert loser[1].http_status == 409
    assert loser[1].code == ErrorCode.INVITATION_INVALID
    assert loser[1].message == "邀请无效或已过期"

    # 恰一 user/会话/outbox/幂等记录；邀请恰消费一次
    assert await _count(pg, "users") == 1
    assert await _count(pg, "sessions") == 1
    assert await _count(pg, "email_outbox") == 1
    assert await _count(pg, "idempotency_records") == 1
    assert await _count(pg, "invitations", "consumed_at IS NOT NULL") == 1


# ---------- 同 key 重放（密码不同）：原响应原样返回，无 Set-Cookie ----------


async def test_same_key_replay_returns_original_response_without_cookies(pg, flow_env):
    token, _ = await _seed_invitation(pg, "replay@example.com")
    async with http_client("10.0.4.1") as client:
        first = await _accept(
            client, token, key="replay-key", email="replay@example.com", password="first-pw"
        )
        assert first.status_code == 200
        second = await _accept(
            client, token, key="replay-key", email="replay@example.com", password="DIFFERENT-pw"
        )

    assert second.status_code == 200
    # 原样重放（password 凭据脱敏不参与 req_hash）：JSON 文档级一致——存量载荷经
    # JSONB 列往返（T5 钉死接口）不保留键序，字节级恒等不可达亦非契约要求
    assert second.json() == first.json()
    assert second.headers.get_list("set-cookie") == []  # 重放契约：不携带 Set-Cookie
    assert await _count(pg, "users") == 1
    assert await _count(pg, "sessions") == 1
    assert await _count(pg, "idempotency_records") == 1
    # 重放不重复业务：库存密码仍是首请求的明文
    u = await _one(pg, "SELECT password_hash FROM users")
    assert verify_password("first-pw", u["password_hash"])


# ---------- users 邮箱唯一：第二封有效邀请接受 → 409 INVITATION_INVALID ----------


async def test_duplicate_user_email_returns_invitation_invalid(pg, flow_env):
    token1, _ = await _seed_invitation(pg, "dupe@example.com")
    async with http_client("10.0.5.1") as client:
        first = await _accept(client, token1, key="dupe-1", email="dupe@example.com")
        assert first.status_code == 200
        token2, iid2 = await _seed_invitation(
            pg, "dupe@example.com"
        )  # 首封已消费，partial 唯一放行
        second = await _accept(client, token2, key="dupe-2", email="dupe@example.com")

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "INVITATION_INVALID"
    assert await _count(pg, "users") == 1
    inv = await _one(pg, "SELECT consumed_at FROM invitations WHERE id = :i", {"i": iid2})
    assert inv["consumed_at"] is None  # 冲突回滚：第二封邀请未被消费


# ---------- HTTP 层限流：第 6 次 accept → 429 + Retry-After ----------


async def test_sixth_accept_rate_limited_429_with_retry_after(pg, flow_env):
    ip = "10.0.6.1"
    async with http_client(ip) as client:
        for i in range(5):
            resp = await _accept(
                client, f"missing-token-{i}", key=f"rl-{i}", email=f"miss{i}@example.com"
            )
            assert resp.status_code == 409  # 业务失败同样计入限流窗口
        sixth = await _accept(client, "missing-token-5", key="rl-5")

    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert 1 <= int(sixth.headers["Retry-After"]) <= 3600
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'invitation_accept' AND subject_hash = :h",
        {"h": hmac_subject("ip", ip)},
    )
    assert n == 5  # 恰 5 行事件（拒绝路径不写）；单主体 [HMAC(ip)]


# ---------- 缺 Idempotency-Key → 400；畸形 email → 400 ----------


async def test_missing_idempotency_key_rejected_400(pg, flow_env):
    token, _ = await _seed_invitation(pg, "nokey@example.com")
    async with http_client("10.0.7.1") as client:
        resp = await _accept(client, token, key="unused", with_key=False)

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await _count(pg, "users") == 0
    assert await _count(pg, "invitations", "consumed_at IS NOT NULL") == 0
    assert await _count(pg, "rate_limit_events") == 0  # 表头校验先于限流与业务


async def test_malformed_email_body_rejected_400(pg, flow_env):
    token, _ = await _seed_invitation(pg, "badmail@example.com")
    async with http_client("10.0.8.1") as client:
        resp = await _accept(client, token, key="bad-mail", email="not-an-email")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await _count(pg, "users") == 0


# ---------- schema 字段边界（review round 1）：超长输入 schema 层截断 ----------


async def test_oversized_password_rejected_400_no_db_side_effects(pg, flow_env):
    """超长 password（2000 > 1024）→ 400；schema 校验短路于限流与业务，零 DB 副作用。"""
    token, _ = await _seed_invitation(pg, "big-pw@example.com")
    async with http_client("10.0.9.1") as client:
        resp = await _accept(
            client, token, key="big-pw", email="big-pw@example.com", password="p" * 2000
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await _count(pg, "rate_limit_events") == 0  # 连限流都未发生
    assert await _count(pg, "users") == 0
    assert await _count(pg, "invitations", "consumed_at IS NOT NULL") == 0


async def test_oversized_invitation_token_rejected_400_no_db_side_effects(pg, flow_env):
    """超长 invitation_token（257 > 256）→ 400 零副作用；256 字符边界过门落入业务 409。"""
    token, _ = await _seed_invitation(pg, "big-tok@example.com")
    async with http_client("10.0.10.1") as client:
        resp = await _accept(client, "t" * 257, key="big-tok", email="big-tok@example.com")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await _count(pg, "rate_limit_events") == 0
        assert await _count(pg, "users") == 0
        assert await _count(pg, "invitations", "consumed_at IS NOT NULL") == 0

        # 边界钉死：256 字符（合法 token 43 字符的宽裕上限）通过 schema 门，进入业务判 409
        edge = await _accept(client, "t" * 256, key="big-tok-edge", email="big-tok@example.com")

    assert edge.status_code == 409
    assert edge.json()["error"]["code"] == "INVITATION_INVALID"
    assert await _count(pg, "rate_limit_events") == 1  # 仅边界请求计入限流窗口


# ---------- email 规范化纯函数 ----------


def test_normalize_email_lowercases_and_rejects_invalid():
    assert invitation_service.normalize_email("  UPPER@Example.COM ") == "upper@example.com"
    with pytest.raises(ValueError):
        invitation_service.normalize_email("not-an-email")
