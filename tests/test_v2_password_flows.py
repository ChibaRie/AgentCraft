"""密码重置（request/confirm）+ 密码修改端到端测试（Task 12）。

契约出处：task-12-brief + Supplement §2 + 裁决 A5/A7/A10/A15；端点契约与事务
语义详见 backend/v2/password_service.py 模块 docstring，此处不重复。覆盖要点：

- request：恒 202（active/suspended/deleting 发信；未知/pending/deleted 零写入，
  响应体逐字节一致防枚举）；限流 3/h 主体 [HMAC(email), HMAC(ip)]，第 4 次 429。
- confirm：幂等重放（§7）、purpose 过滤（email_verify 令牌不得驱动重置）、失效
  形态统一 400「链接无效或已过期」逐字节一致、全链改密 + 全会话失效 + 重新登录、
  suspended 账号可重置、30 分钟有效期、缺 Idempotency-Key 400。
- change：保留当前会话撤销其余、错当前密码 401、TOTP 门（A15 先于 Argon2；缺码
  /错码 400 MFA_INVALID「验证码无效」；第 6 次失败 429）。

HTTP 层沿用 T10/T11 house pattern（ASGITransport 于测试自身循环驱动真实 app；
http_client 复用自 tests/test_v2_verification_flow.py）。本文件自带 _auth_client
（不复用 T10 authenticated_client）：T10 版播种 password_hash='h' 字面量，无法
通过 Argon2 验密；密码链路需要真实哈希 + 可选 TOTP 信封种子（T11 同款）。
密钥注入：RATE_LIMIT_HMAC_KEY / EMAIL_OUTBOX_ENCRYPTION_KEY / MFA_ENCRYPTION_KEY
经 monkeypatch.setenv 注入一次性 b64url 材料；reset 令牌明文经同材料 keyring
解密 outbox payload 取出（AAD 绑定行 id，全链实证）。种子/复核一律 superuser。
"""

import base64
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pyotp
import pytest
from sqlalchemy import text

from backend.main import app
from backend.utils.crypto import decrypt_text, encrypt_text, make_keyring
from backend.v2 import login_service
from backend.v2.rate_limit import hmac_subject
from backend.v2.runtime import get_v2_runtime, owner_session
from backend.v2.security import generate_token, hash_password, hash_token
from backend.v2.session_service import create_session, list_sessions
from tests.conftest import PgDb
from tests.test_v2_runtime import make_v2_runtime
from tests.test_v2_verification_flow import http_client

_ROUTE_RESET_REQUEST = "/api/v2/auth/password-reset/request"
_ROUTE_RESET_CONFIRM = "/api/v2/auth/password-reset/confirm"
_ROUTE_PASSWORD_CHANGE = "/api/v2/auth/password-change"
_ROUTE_LOGIN = "/api/v2/auth/login"
_KEY_MATERIAL = base64.urlsafe_b64encode(bytes(range(32))).decode()  # 仅测试材料
_UA = "AgentCraft-FlowTest/1.0"

# 统一失败文案（契约钉死；响应体形状见 backend/main.py 错误处理器）
_ACCEPTED_202 = {"data": {"accepted": True}}
_OK_200 = {"data": {"ok": True}}
_UNIFORM_400 = {"error": {"code": "EMAIL_NOT_VERIFIED", "message": "链接无效或已过期"}}
_INVALID_CURRENT_401 = {"error": {"code": "INVALID_CREDENTIALS", "message": "当前密码不正确"}}
_MFA_INVALID_400 = {"error": {"code": "MFA_INVALID", "message": "验证码无效"}}


@pytest.fixture
async def flow_env(pg: PgDb, monkeypatch):
    """app/admin 双 role runtime + FastAPI 依赖 override + 三密钥注入；yield runtime。"""
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("EMAIL_OUTBOX_ENCRYPTION_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("MFA_ENCRYPTION_KEY", _KEY_MATERIAL)
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield rt
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.close()  # 同步 dispose 双引擎连接池（sync_engine.dispose）


@pytest.fixture(autouse=True)
def _reset_mfa_stores():
    """A13：login 端点在本套件被复用（新密码可登录断言）——挑战 store 用例前后清空。"""
    login_service.challenge_store.reset()
    login_service.pending_mfa_secrets.reset()
    yield
    login_service.challenge_store.reset()
    login_service.pending_mfa_secrets.reset()


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


def _mfa_envelope(secret: str, user_id: str) -> str:
    """测试 keyring 直接构造 TOTP 信封（材料与 flow_env 注入 Settings 的同源）。"""
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
    totp_secret: str | None = None,
) -> str:
    """播种用户行（真实 Argon2 哈希 + 可选 TOTP 信封），返回 user id 字符串。"""
    new_id = uuid.uuid4()
    enc = _mfa_envelope(totp_secret, str(new_id)) if totp_secret is not None else None
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc) "
                "VALUES (:i, :e, :p, 'user', :s, :m)"
            ),
            {"i": str(new_id), "e": email, "p": hash_password(password), "s": status, "m": enc},
        )
    return str(new_id)


async def _seed_reset_token(
    pg: PgDb,
    user_id: str,
    *,
    purpose: str = "password_reset",
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
                "exp": now - timedelta(minutes=10) if expired else now + timedelta(hours=1),
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


async def _reset_token_from_outbox(pg: PgDb, user_id: str) -> str:
    """解密 outbox payload 取明文 reset 令牌（同材料 keyring；AAD 绑定行 id）。"""
    row = await _one(
        pg,
        "SELECT id, payload_ciphertext FROM email_outbox "
        "WHERE user_id = :u AND purpose = 'password_reset' ORDER BY created_at DESC LIMIT 1",
        {"u": user_id},
    )
    _, keyring = make_keyring(f"primary:{_KEY_MATERIAL}")
    payload = json.loads(
        decrypt_text(
            json.loads(row["payload_ciphertext"]),
            aad=f"agentcraft:email_outbox:{row['id']}:v1",
            keyring=keyring,
        )
    )
    return payload["vars"]["action_token"]


@asynccontextmanager
async def _auth_client(
    pg: PgDb,
    rt,
    *,
    email: str,
    password: str = "pw-123456",
    status: str = "active",
    totp_secret: str | None = None,
    ip: str = "10.0.50.0",
    device: str = _UA,
):
    """带真实密码（可验密）会话的认证 HTTP 客户端；yield (client, csrf, uid, 会话明文)。

    superuser 播种用户（真实 Argon2 哈希 + 可选 TOTP 信封）→ owner 事务内
    create_session → AsyncClient 预置 ac_session cookie。POST 须携带
    ``X-CSRF-Token: csrf``。
    """
    uid = await _seed_user(pg, email, password=password, status=status, totp_secret=totp_secret)
    async with owner_session(rt, uid) as db:
        session_token, csrf_token = await create_session(
            db, user_id=uuid.UUID(uid), device_label=device
        )
    async with http_client(ip, cookies={"ac_session": session_token}) as client:
        yield client, csrf_token, uid, session_token


async def _extra_session(rt, user_id: str, device: str = "AgentCraft-Other/1.0") -> tuple[str, str]:
    """owner 事务内为既有用户再建一个会话（多设备场景）；返回 (会话明文, csrf 明文)。"""
    async with owner_session(rt, user_id) as db:
        return await create_session(db, user_id=uuid.UUID(user_id), device_label=device)


async def _session_id_by_token(pg: PgDb, session_token: str) -> str:
    row = await _one(
        pg, "SELECT id FROM sessions WHERE token_hash = :h", {"h": hash_token(session_token)}
    )
    return str(row["id"])


async def _session_revoked(pg: PgDb, session_token: str) -> bool:
    row = await _one(
        pg,
        "SELECT revoked_at FROM sessions WHERE token_hash = :h",
        {"h": hash_token(session_token)},
    )
    return row["revoked_at"] is not None


def _current_code(secret: str) -> str:
    """当前 30s 窗口的有效 TOTP 码（pyotp .at 免睡眠）。"""
    return pyotp.TOTP(secret).at(datetime.now(timezone.utc))


def _wrong_code(secret: str) -> str:
    """与当前时间码必然不同的 6 位码（排除万年一遇的假阴性）。"""
    current = _current_code(secret)
    return "000000" if current != "000000" else "000001"


async def _login_status(email: str, password: str, ip: str) -> httpx.Response:
    """独立客户端登录一次（新密码可用/旧密码失效断言用）。"""
    async with http_client(ip) as client:
        return await client.post(_ROUTE_LOGIN, json={"email": email, "password": password})


# ---------- request：恒 202 + 状态矩阵 ----------


async def test_reset_request_status_matrix_issues_token_and_invalidates_prior(pg, flow_env):
    """active/suspended/deleting 均发信（status IN 列表）；旧未消费令牌整批作废；
    新令牌 30 分钟有效期；email 大小写规范化命中同一账号。"""
    active_uid = await _seed_user(pg, "reset-active@example.com", password="old-pw-1")
    old_token = await _seed_reset_token(pg, active_uid)
    suspended_uid = await _seed_user(pg, "reset-susp@example.com", status="suspended")
    deleting_uid = await _seed_user(pg, "reset-del@example.com", status="deleting")

    async with http_client("10.0.40.1") as client:
        for i, email in enumerate(
            ["Reset-Active@Example.com", "reset-susp@example.com", "reset-del@example.com"]
        ):
            resp = await client.post(_ROUTE_RESET_REQUEST, json={"email": email})
            assert resp.status_code == 202, i
            assert resp.json() == _ACCEPTED_202

    # 限流主体用规范化小写 email；组合主体逐 subject 各写一行（T11 语义）
    n_email = await _count(
        pg,
        "rate_limit_events",
        "scope = 'password_reset_request' AND subject_hash = :h",
        {"h": hmac_subject("email", "reset-active@example.com")},
    )
    n_ip = await _count(
        pg,
        "rate_limit_events",
        "scope = 'password_reset_request' AND subject_hash = :h",
        {"h": hmac_subject("ip", "10.0.40.1")},
    )
    assert n_email == 1 and n_ip == 3

    # 旧未消费令牌被作废；新令牌未消费且 30 分钟有效期（DB 时钟差值断言）
    rows = await _all(
        pg,
        "SELECT token_hash, consumed_at, expires_at, now() AS db_now "
        "FROM account_action_tokens WHERE user_id = :u AND purpose = 'password_reset'",
        {"u": active_uid},
    )
    assert len(rows) == 2
    old_rows = [r for r in rows if r["token_hash"] == hash_token(old_token)]
    fresh = [r for r in rows if r["token_hash"] != hash_token(old_token)]
    assert len(old_rows) == 1 and old_rows[0]["consumed_at"] is not None  # 旧令牌作废
    assert len(fresh) == 1 and fresh[0]["consumed_at"] is None
    diff = fresh[0]["expires_at"] - fresh[0]["db_now"]
    assert timedelta(minutes=29) < diff <= timedelta(minutes=30)

    # suspended/deleting 同样发信（owner 单事务：令牌 + outbox 原子落库）
    for uid in (suspended_uid, deleting_uid):
        for table in ("email_outbox", "account_action_tokens"):
            where = "user_id = :u AND purpose = 'password_reset'"
            assert await _count(pg, table, where, {"u": uid}) == 1


async def test_reset_request_202_byte_identical_zero_writes_for_unknown_pending_deleted(
    pg, flow_env
):
    """恒 202 防枚举：已知 active / 未知 / pending / deleted 响应体逐字节一致；
    非可重置状态零写入（无令牌、无 outbox 行）。"""
    active_uid = await _seed_user(pg, "known@example.com")
    pending_uid = await _seed_user(pg, "pending@example.com", status="pending")
    deleted_uid = await _seed_user(pg, "gone@example.com", status="deleted")

    requests = [
        ("known_active", {"email": "known@example.com"}, "10.0.41.1"),
        ("unknown", {"email": "ghost@example.com"}, "10.0.41.2"),
        ("pending", {"email": "pending@example.com"}, "10.0.41.3"),
        ("deleted", {"email": "gone@example.com"}, "10.0.41.4"),
    ]
    bodies = []
    for mode, payload, ip in requests:
        async with http_client(ip) as client:
            resp = await client.post(_ROUTE_RESET_REQUEST, json=payload)
        assert resp.status_code == 202, mode
        assert resp.json() == _ACCEPTED_202, mode
        bodies.append(resp.content)
    assert len(set(bodies)) == 1  # 防枚举：响应体逐字节一致

    # 恰 active 用户有令牌与 outbox 行；其余零写入
    assert await _count(pg, "account_action_tokens", "purpose = 'password_reset'") == 1
    assert await _count(pg, "email_outbox", "purpose = 'password_reset'") == 1
    ob = await _one(pg, "SELECT user_id FROM email_outbox WHERE purpose = 'password_reset'")
    assert str(ob["user_id"]) == active_uid
    for uid in (pending_uid, deleted_uid):
        assert await _count(pg, "account_action_tokens", "user_id = :u", {"u": uid}) == 0
        assert await _count(pg, "email_outbox", "user_id = :u", {"u": uid}) == 0


async def test_reset_request_fourth_attempt_429_with_retry_after(pg, flow_env):
    """password_reset_request 3/h：第 4 次同 email+IP → 429 + Retry-After；
    拒绝路径不写事件；未知邮箱零业务写入（限流先于查找，且不泄漏账号存在性）。"""
    payload = {"email": "rl-reset@example.com"}
    async with http_client("10.0.42.1") as client:
        for _ in range(3):
            resp = await client.post(_ROUTE_RESET_REQUEST, json=payload)
            assert resp.status_code == 202
        fourth = await client.post(_ROUTE_RESET_REQUEST, json=payload)

    assert fourth.status_code == 429
    assert fourth.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert 1 <= int(fourth.headers["Retry-After"]) <= 3600
    n_email = await _count(
        pg,
        "rate_limit_events",
        "scope = 'password_reset_request' AND subject_hash = :h",
        {"h": hmac_subject("email", "rl-reset@example.com")},
    )
    n_ip = await _count(
        pg,
        "rate_limit_events",
        "scope = 'password_reset_request' AND subject_hash = :h",
        {"h": hmac_subject("ip", "10.0.42.1")},
    )
    assert n_email == 3 and n_ip == 3  # 拒绝路径不写
    assert await _count(pg, "email_outbox") == 0
    assert await _count(pg, "account_action_tokens") == 0


# ---------- confirm：全链 + 幂等重放 ----------


async def test_confirm_full_chain_password_changed_and_all_sessions_revoked(pg, flow_env):
    """request → outbox 解密取令牌 → confirm：密码改换、全部既有会话失效、
    旧密码 401 / 新密码可登录（重新登录强制）、令牌单次消费、幂等记录同事务落库。"""
    uid = await _seed_user(pg, "chain@example.com", password="old-pass-1")
    token_s1, _ = await _extra_session(flow_env, uid, device="AgentCraft-Device1/1.0")
    token_s2, _ = await _extra_session(flow_env, uid, device="AgentCraft-Device2/1.0")

    async with http_client("10.0.43.1") as client:
        req = await client.post(_ROUTE_RESET_REQUEST, json={"email": "chain@example.com"})
        assert req.status_code == 202
    reset_token = await _reset_token_from_outbox(pg, uid)

    async with http_client("10.0.43.2") as client:
        resp = await client.post(
            _ROUTE_RESET_CONFIRM,
            json={"reset_token": reset_token, "new_password": "new-pass-9"},
            headers={"Idempotency-Key": "confirm-chain"},
        )

    assert resp.status_code == 200
    assert resp.json() == _OK_200
    assert resp.headers.get_list("set-cookie") == []  # 公开端点不种 cookie

    # 全部既有会话软撤销（Supplement：成功撤销该用户全部会话并要求重新登录）
    for st in (token_s1, token_s2):
        assert await _session_revoked(pg, st), st
    at = await _one(
        pg, "SELECT consumed_at FROM account_action_tokens WHERE user_id = :u", {"u": uid}
    )
    assert at["consumed_at"] is not None  # 令牌恰消费一次

    # 旧密码失效（统一 401）；新密码可登录建会话
    old_login = await _login_status("chain@example.com", "old-pass-1", "10.0.43.3")
    new_login = await _login_status("chain@example.com", "new-pass-9", "10.0.43.4")
    assert old_login.status_code == 401
    assert old_login.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert new_login.status_code == 200

    rec = await _one(pg, "SELECT status_code, response_json FROM idempotency_records")
    assert rec["status_code"] == 200
    assert rec["response_json"] == _OK_200


async def test_confirm_replay_same_key_returns_original_200(pg, flow_env):
    """同 Idempotency-Key 重放 → 原样 200（begin 命中先于一切状态检查，§7）。"""
    uid = await _seed_user(pg, "replay@example.com", password="pw-replay")
    async with http_client("10.0.44.1") as client:
        first_req = await client.post(_ROUTE_RESET_REQUEST, json={"email": "replay@example.com"})
        assert first_req.status_code == 202
    reset_token = await _reset_token_from_outbox(pg, uid)
    payload = {"reset_token": reset_token, "new_password": "new-pass-1"}
    async with http_client("10.0.44.2") as client:
        first = await client.post(
            _ROUTE_RESET_CONFIRM, json=payload, headers={"Idempotency-Key": "replay-1"}
        )
        second = await client.post(
            _ROUTE_RESET_CONFIRM, json=payload, headers={"Idempotency-Key": "replay-1"}
        )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == _OK_200
    assert await _count(pg, "idempotency_records") == 1  # 重放不新增
    consumed = await _count(
        pg, "account_action_tokens", "user_id = :u AND consumed_at IS NOT NULL", {"u": uid}
    )
    assert consumed == 1


# ---------- confirm：失效形态统一 400 逐字节一致 + 零副作用 ----------


async def _uid_by_email(pg: PgDb, email: str) -> str:
    row = await _one(pg, "SELECT id FROM users WHERE email = :e", {"e": email})
    return str(row["id"])


async def test_confirm_failure_modes_uniform_400_byte_identical(pg, flow_env):
    """未知/过期/跨 purpose/已消费令牌统一 400 EMAIL_NOT_VERIFIED（防探测）；
    失败路径零副作用（令牌不消费、密码不变、无幂等记录、无会话）。"""
    expired_uid = await _seed_user(pg, "cf-exp@example.com", password="pw-1")
    expired_token = await _seed_reset_token(pg, expired_uid, expired=True)
    purpose_uid = await _seed_user(pg, "cf-purpose@example.com", password="pw-2")
    email_token = await _seed_reset_token(pg, purpose_uid, purpose="email_verify")
    consumed_uid = await _seed_user(pg, "cf-used@example.com", password="pw-3")
    used_token = await _seed_reset_token(pg, consumed_uid, consumed=True)
    hashes_before = {
        str(r["id"]): r["password_hash"]
        for r in await _all(pg, "SELECT id, password_hash FROM users")
    }

    requests = [
        ("unknown_token", generate_token()),
        ("expired_token", expired_token),
        ("wrong_purpose", email_token),
        ("already_consumed", used_token),
    ]
    bodies = []
    async with http_client("10.0.45.1") as client:
        for i, (mode, token) in enumerate(requests):
            resp = await client.post(
                _ROUTE_RESET_CONFIRM,
                json={"reset_token": token, "new_password": "whatever-1"},
                headers={"Idempotency-Key": f"cf-uniform-{i}"},
            )
            assert resp.status_code == 400, mode
            assert resp.json() == _UNIFORM_400, mode
            bodies.append(resp.content)
    assert len(set(bodies)) == 1  # 全部失效形态响应体逐字节一致

    rows = await _all(pg, "SELECT token_hash, consumed_at FROM account_action_tokens")
    # 令牌分文不消费（种子时已消费的那枚除外——播种态，非业务写入）
    assert all(r["consumed_at"] is None for r in rows if r["token_hash"] != hash_token(used_token))
    hashes_after = {
        str(r["id"]): r["password_hash"]
        for r in await _all(pg, "SELECT id, password_hash FROM users")
    }
    assert hashes_after == hashes_before  # 密码未变
    assert await _count(pg, "sessions") == 0
    assert await _count(pg, "idempotency_records") == 0  # 失败路径不写幂等记录


async def test_confirm_email_verify_token_cannot_drive_reset(pg, flow_env):
    """purpose 过滤钉死：email_verify 令牌不得驱动密码重置（统一 400，零副作用）。"""
    uid = await _seed_user(pg, "xp@example.com", password="old-pass-x")
    before = await _one(pg, "SELECT password_hash FROM users WHERE id = :u", {"u": uid})
    token = await _seed_reset_token(pg, uid, purpose="email_verify")
    async with http_client("10.0.46.1") as client:
        resp = await client.post(
            _ROUTE_RESET_CONFIRM,
            json={"reset_token": token, "new_password": "new-pass-x"},
            headers={"Idempotency-Key": "xp-1"},
        )

    assert resp.status_code == 400
    assert resp.json() == _UNIFORM_400
    after = await _one(pg, "SELECT password_hash FROM users WHERE id = :u", {"u": uid})
    assert after["password_hash"] == before["password_hash"]  # 密码未变
    row = await _one(
        pg, "SELECT consumed_at FROM account_action_tokens WHERE user_id = :u", {"u": uid}
    )
    assert row["consumed_at"] is None  # 令牌未被误消费


async def test_confirm_suspended_account_resets_password(pg, flow_env):
    """suspended 账号可经重置链路改密（status IN ('active','suspended','deleting')）；
    状态保持 suspended；既有会话全部失效。"""
    uid = await _seed_user(pg, "susp@example.com", password="old-susp", status="suspended")
    before = await _one(pg, "SELECT password_hash FROM users WHERE id = :u", {"u": uid})
    token_s, _ = await _extra_session(flow_env, uid)
    token = await _seed_reset_token(pg, uid)
    async with http_client("10.0.47.1") as client:
        resp = await client.post(
            _ROUTE_RESET_CONFIRM,
            json={"reset_token": token, "new_password": "new-susp-1"},
            headers={"Idempotency-Key": "susp-1"},
        )

    assert resp.status_code == 200
    assert resp.json() == _OK_200
    after = await _one(pg, "SELECT password_hash, status FROM users WHERE id = :u", {"u": uid})
    assert after["password_hash"] != before["password_hash"]  # 密码已换
    assert after["status"] == "suspended"  # 状态不变
    assert await _session_revoked(pg, token_s)


async def test_confirm_missing_idempotency_key_rejected_400(pg, flow_env):
    uid = await _seed_user(pg, "nokey@example.com")
    token = await _seed_reset_token(pg, uid)
    async with http_client("10.0.48.1") as client:
        resp = await client.post(
            _ROUTE_RESET_CONFIRM, json={"reset_token": token, "new_password": "new-pass-1"}
        )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    row = await _one(
        pg, "SELECT consumed_at FROM account_action_tokens WHERE user_id = :u", {"u": uid}
    )
    assert row["consumed_at"] is None
    assert await _count(pg, "idempotency_records") == 0


async def test_schema_bounds_zero_side_effects(pg, flow_env):
    """schema 边界（超长 reset_token / new_password、短 totp_code）→ 400
    VALIDATION_ERROR，零 DB 副作用（schema 门先于幂等/限流/业务）。"""
    async with _auth_client(
        pg, flow_env, email="bounds@example.com", password="pw-bounds", ip="10.0.49.1"
    ) as (client, csrf, uid, _tok):
        await _seed_reset_token(pg, uid)
        big_pw = await client.post(
            _ROUTE_PASSWORD_CHANGE,
            json={"current_password": "pw-bounds", "new_password": "x" * 1025},
            headers={"X-CSRF-Token": csrf},
        )
        short_code = await client.post(
            _ROUTE_PASSWORD_CHANGE,
            json={
                "current_password": "pw-bounds",
                "new_password": "ok-pass-1",
                "totp_code": "12345",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert big_pw.status_code == short_code.status_code == 400
        for resp in (big_pw, short_code):
            assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    async with http_client("10.0.49.2") as client:
        big_token = await client.post(
            _ROUTE_RESET_CONFIRM,
            json={"reset_token": "t" * 257, "new_password": "ok-pass-1"},
            headers={"Idempotency-Key": "bounds-token"},
        )
    assert big_token.status_code == 400
    assert big_token.json()["error"]["code"] == "VALIDATION_ERROR"

    assert await _count(pg, "rate_limit_events") == 0  # schema 门先于限流与业务
    assert await _count(pg, "idempotency_records") == 0
    row = await _one(
        pg, "SELECT consumed_at FROM account_action_tokens WHERE user_id = :u", {"u": uid}
    )
    assert row["consumed_at"] is None  # 真实令牌未被误消费
    still = await _login_status("bounds@example.com", "pw-bounds", "10.0.49.3")
    assert still.status_code == 200  # 密码未被越界请求改动


# ---------- change：保留当前会话 / 撤销其余 ----------


async def test_change_keeps_current_session_revokes_others(pg, flow_env):
    async with _auth_client(
        pg, flow_env, email="change@example.com", password="old-pass-1", ip="10.0.50.1"
    ) as (client, csrf, uid, current_token):
        other_token, _ = await _extra_session(flow_env, uid, device="AgentCraft-Other/1.0")
        current_sid = await _session_id_by_token(pg, current_token)
        async with owner_session(flow_env, uid) as db:
            before = await list_sessions(db, uuid.UUID(current_sid))
        assert len(before) == 2 and sum(1 for r in before if r["current"]) == 1

        resp = await client.post(
            _ROUTE_PASSWORD_CHANGE,
            json={"current_password": "old-pass-1", "new_password": "new-pass-1"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200
        assert resp.json() == _OK_200

        assert await _session_revoked(pg, other_token)  # 其余会话撤销
        assert not await _session_revoked(pg, current_token)  # 当前会话保留

        # 当前会话仍然有效：以新密码再次变更成功（get_v2_auth 门序通过）
        second = await client.post(
            _ROUTE_PASSWORD_CHANGE,
            json={"current_password": "new-pass-1", "new_password": "new-pass-2"},
            headers={"X-CSRF-Token": csrf},
        )
        assert second.status_code == 200

        async with owner_session(flow_env, uid) as db:
            after = await list_sessions(db, uuid.UUID(current_sid))
        assert len(after) == 2  # 撤销行保留（设备列表展示语义）


async def test_change_wrong_current_password_401_uniform(pg, flow_env):
    async with _auth_client(
        pg, flow_env, email="wrongcur@example.com", password="right-pass-1", ip="10.0.51.1"
    ) as (client, csrf, _uid, current_token):
        resp = await client.post(
            _ROUTE_PASSWORD_CHANGE,
            json={"current_password": "wrong-pass", "new_password": "new-pass-1"},
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 401
    assert resp.json() == _INVALID_CURRENT_401
    assert not await _session_revoked(pg, current_token)  # 会话不受影响
    still = await _login_status("wrongcur@example.com", "right-pass-1", "10.0.51.2")
    assert still.status_code == 200  # 密码未变


# ---------- change：TOTP 门（A15：先于 Argon2）----------


async def test_change_totp_missing_code_400_mfa_invalid(pg, flow_env):
    """TOTP 已启用者缺 totp_code → 400 MFA_INVALID「验证码无效」，计入
    password_change_totp 窗口；密码未变、当前会话保留。"""
    secret = pyotp.random_base32()
    async with _auth_client(
        pg,
        flow_env,
        email="totpmiss@example.com",
        password="pw-1",
        totp_secret=secret,
        ip="10.0.52.1",
    ) as (client, csrf, uid, current_token):
        resp = await client.post(
            _ROUTE_PASSWORD_CHANGE,
            json={"current_password": "pw-1", "new_password": "new-pass-1"},  # 无 totp_code
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 400
    assert resp.json() == _MFA_INVALID_400
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'password_change_totp' AND subject_hash = :h",
        {"h": hmac_subject("user", uid)},
    )
    assert n == 1  # 缺码同样计入窗口
    still = await _login_status("totpmiss@example.com", "pw-1", "10.0.52.2")
    assert still.status_code == 200 and still.json()["data"]["mfa_required"] is True  # 密码未变
    assert not await _session_revoked(pg, current_token)


async def test_change_totp_wrong_code_sixth_attempt_429(pg, flow_env):
    """password_change_totp 5/900s：5 次错码 400（逐字节一致），第 6 次 429 +
    Retry-After；拒绝路径不写事件；密码从未被改动。"""
    secret = pyotp.random_base32()
    async with _auth_client(
        pg,
        flow_env,
        email="totprl@example.com",
        password="pw-1",
        totp_secret=secret,
        ip="10.0.53.1",
    ) as (client, csrf, uid, _tok):
        wrong = _wrong_code(secret)
        bodies = []
        for _ in range(5):
            resp = await client.post(
                _ROUTE_PASSWORD_CHANGE,
                json={
                    "current_password": "pw-1",
                    "new_password": "new-pass-1",
                    "totp_code": wrong,
                },
                headers={"X-CSRF-Token": csrf},
            )
            assert resp.status_code == 400
            assert resp.json() == _MFA_INVALID_400
            bodies.append(resp.content)
        sixth = await client.post(
            _ROUTE_PASSWORD_CHANGE,
            json={"current_password": "pw-1", "new_password": "new-pass-1", "totp_code": wrong},
            headers={"X-CSRF-Token": csrf},
        )

    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert 1 <= int(sixth.headers["Retry-After"]) <= 900
    assert len(set(bodies)) == 1  # 五次失败响应体逐字节一致
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'password_change_totp' AND subject_hash = :h",
        {"h": hmac_subject("user", uid)},
    )
    assert n == 5  # 拒绝路径不写
    still = await _login_status("totprl@example.com", "pw-1", "10.0.53.2")
    assert still.status_code == 200  # 密码从未被改动


async def test_change_totp_checked_before_current_password_pins_a15(pg, flow_env):
    """A15 排序钉死：错 TOTP + 错当前密码 → 400 MFA_INVALID（而非 401
    INVALID_CREDENTIALS）——TOTP 校验先于 Argon2 昂贵计算。"""
    secret = pyotp.random_base32()
    async with _auth_client(
        pg,
        flow_env,
        email="totporder@example.com",
        password="right-pass",
        totp_secret=secret,
        ip="10.0.54.1",
    ) as (client, csrf, uid, _tok):
        resp = await client.post(
            _ROUTE_PASSWORD_CHANGE,
            json={
                "current_password": "deliberately-wrong",
                "new_password": "new-pass-1",
                "totp_code": _wrong_code(secret),
            },
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 400
    assert resp.json() == _MFA_INVALID_400
    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'password_change_totp' AND subject_hash = :h",
        {"h": hmac_subject("user", uid)},
    )
    assert n == 1


async def test_change_with_totp_correct_code_succeeds(pg, flow_env):
    """TOTP + 当前密码均正确 → 200；新密码生效（TOTP 用户登录走挑战路径，
    200 mfa_required 即验密通过）；其余会话撤销、当前保留；无失败限流事件。"""
    secret = pyotp.random_base32()
    async with _auth_client(
        pg,
        flow_env,
        email="totpok@example.com",
        password="old-pass-1",
        totp_secret=secret,
        ip="10.0.55.1",
    ) as (client, csrf, uid, current_token):
        other_token, _ = await _extra_session(flow_env, uid, device="AgentCraft-Other/1.0")
        resp = await client.post(
            _ROUTE_PASSWORD_CHANGE,
            json={
                "current_password": "old-pass-1",
                "new_password": "new-pass-1",
                "totp_code": _current_code(secret),
            },
            headers={"X-CSRF-Token": csrf},
        )

    assert resp.status_code == 200
    assert resp.json() == _OK_200
    relogin = await _login_status("totpok@example.com", "new-pass-1", "10.0.55.2")
    assert relogin.status_code == 200
    assert relogin.json()["data"]["mfa_required"] is True  # 新密码验证通过
    assert await _session_revoked(pg, other_token)  # 其余会话撤销
    assert not await _session_revoked(pg, current_token)  # 当前会话保留
    assert await _count(pg, "rate_limit_events", "scope = 'password_change_totp'") == 0
