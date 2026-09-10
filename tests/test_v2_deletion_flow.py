"""账户注销（request/cancel/status）+ 到期清理端到端测试（Task 13）。

契约出处：task-13-brief + Supplement §2 + 裁决 A10/A12/A15；端点契约与事务语义
详见 backend/v2/deletion_service.py 模块 docstring，此处不重复。覆盖要点：

- request：非 active 拒绝矩阵（pending 经 HTTP 409 ACCOUNT_PENDING；suspended 403
  /deleting 409 服务层状态门——HTTP 面 get_v2_auth 先行拦截/会话已失效，A10）；
  再认证失败（错密码 401；TOTP 错码/缺码 400 MFA_INVALID + A15 排序钉死，失败计入
  password_change_totp）；成功 → status deleting + deadline ~14d + 全会话失效
  （含当前）+ TERMINATE_TASKS_HOOK 被调 + cancel 令牌 + outbox 行；enqueue 注入
  失败 → 整体回滚 status 仍 active；同 key 重放先于状态（cancel 恢复 active 后
  仍回放原 200）。
- cancel：成功恢复 active + deadline 置空 + 全新会话（双 cookie）+ 令牌恰消费 +
  同 key 重放不重复建会话（A12）；失效形态（过期/已消费/非 deleting/未知）统一
  409 逐字节一致（防探测）；伪造 token 高频触发 deletion_cancel_invalid [ip]
  429；合法路径第 11 次触发 deletion_cancel_attempt 429（分桶计数）。
- status：active 用户 200 形状钉死（days_remaining/deadline_at 均 null）；deleting
  用户因全会话失效 401（A10 语义）；视图函数 deleting 形状直接钉死。
- sweep：到期 deleting → deleted + 匿名化（email 换 deleted+<id>@users.invalid、
  mfa 清空、密码不可验）+ deadline/deleted_at 收口 + 全会话失效 + 审计引用保留
  （users 行不删除）；未到期 deleting 不处理。

HTTP 层沿用 T10-T12 house pattern（ASGITransport 于测试自身循环驱动真实 app）。
种子/复核助手复用 T12（真实 Argon2 哈希 + TOTP 信封种子——T10 authenticated_client
播种 'h' 字面量无法过 Argon2 验密；密码链路需要真实哈希）。密钥注入：复用 T12
flow_env（RATE_LIMIT_HMAC_KEY / EMAIL_OUTBOX_ENCRYPTION_KEY / MFA_ENCRYPTION_KEY
同源材料，与 _mfa_envelope / outbox 解密 keyring 一致）；cancel 令牌明文经同材料
keyring 解密 outbox payload 取出（AAD 绑定行 id，全链实证）。种子/复核一律
superuser（pg.engine，绕 owner-RLS）。
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pyotp
import pytest
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.main import app
from backend.utils.crypto import decrypt_text, make_keyring
from backend.v2 import deletion_service
from backend.v2.rate_limit import hmac_subject
from backend.v2.runtime import get_v2_runtime, owner_session
from backend.v2.security import generate_token, hash_password, verify_password
from backend.v2.session_service import create_session
from tests.conftest import PgDb
from tests.test_v2_password_flows import (
    _KEY_MATERIAL,
    _all,
    _auth_client,
    _count,
    _current_code,
    _extra_session,
    _mfa_envelope,
    _one,
    _seed_reset_token,
    _seed_user,
    _session_revoked,
    _wrong_code,
    http_client,
)
from tests.test_v2_runtime import make_v2_runtime

_ROUTE_REQUEST = "/api/v2/account/deletion/request"
_ROUTE_CANCEL = "/api/v2/account/deletion/cancel"
_ROUTE_STATUS = "/api/v2/account/deletion/status"
_UA = "AgentCraft-FlowTest/1.0"

# 统一成功/失败载荷（契约钉死；响应体形状见 backend/main.py 错误处理器）
_REQUEST_200 = {"data": {"status": "deleting", "days_remaining": 14}}
_STATUS_200 = {"data": {"status": "active", "days_remaining": None, "deadline_at": None}}
_PENDING_409 = {"error": {"code": "ACCOUNT_PENDING", "message": "账户尚未完成邮箱验证"}}
_SUSPENDED_403 = {"error": {"code": "ACCOUNT_SUSPENDED", "message": "账户已被停用"}}
_DELETING_409 = {"error": {"code": "ACCOUNT_DELETING", "message": "注销处理中"}}
_INVALID_CURRENT_401 = {"error": {"code": "INVALID_CREDENTIALS", "message": "当前密码不正确"}}
_MFA_INVALID_400 = {"error": {"code": "MFA_INVALID", "message": "验证码无效"}}
_INVALID_CANCEL_409 = {"error": {"code": "ACCOUNT_DELETING", "message": "撤销链接无效或已过期"}}


@pytest.fixture
async def flow_env(pg: PgDb, monkeypatch):
    """app/admin 双 role runtime + FastAPI 依赖 override + 三密钥注入；yield runtime。

    与 T12 flow_env 同款（材料常量同源，_mfa_envelope / outbox 解密 keyring 一致）；
    独立定义以避免测试模块间 fixture 名 re-export 的遮蔽歧义。
    """
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("EMAIL_OUTBOX_ENCRYPTION_KEY", _KEY_MATERIAL)
    monkeypatch.setenv("MFA_ENCRYPTION_KEY", _KEY_MATERIAL)
    rt = make_v2_runtime(pg)
    app.dependency_overrides[get_v2_runtime] = lambda: rt
    yield rt
    app.dependency_overrides.pop(get_v2_runtime, None)
    rt.close()  # 同步 dispose 双引擎连接池（sync_engine.dispose）


# ---------- 本任务种子/复核助手（superuser 绕 RLS；其余复用 T12）----------


async def _cancel_token_from_outbox(pg: PgDb, user_id: str) -> str:
    """解密 outbox payload 取明文 cancel 令牌（同材料 keyring；AAD 绑定行 id）。"""
    row = await _one(
        pg,
        "SELECT id, payload_ciphertext FROM email_outbox "
        "WHERE user_id = :u AND purpose = 'deletion_cancel' ORDER BY created_at DESC LIMIT 1",
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


async def _seed_deleting_user(
    pg: PgDb, email: str, *, password: str, expired: bool, totp_secret: str | None = None
) -> str:
    """播种 deleting 用户（deadline 已过/未过；可选 TOTP 信封），返回 user id。"""
    new_id = uuid.uuid4()
    enc = _mfa_envelope(totp_secret, str(new_id)) if totp_secret is not None else None
    now = datetime.now(timezone.utc)
    deadline = now - timedelta(hours=1) if expired else now + timedelta(days=3)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc, "
                "deletion_deadline_at) VALUES (:i, :e, :p, 'user', 'deleting', :m, :d)"
            ),
            {"i": str(new_id), "e": email, "p": hash_password(password), "m": enc, "d": deadline},
        )
    return str(new_id)


async def _seed_audit_log(pg: PgDb, actor_id: str) -> str:
    """播种 audit_logs 行（actor_id 指向待清理用户），返回行 id。"""
    async with pg.engine.begin() as conn:
        return str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO audit_logs (id, actor_id, action, target_type, reason) "
                        "VALUES (gen_random_uuid(), :a, 'test.sweep', 'user', 'sweep-test') "
                        "RETURNING id"
                    ),
                    {"a": actor_id},
                )
            ).scalar_one()
        )


def _session_cookie_from(response: httpx.Response) -> str:
    """从 Set-Cookie 头提取 ac_session 明文（SESSION_COOKIE_SECURE=true 下 httpx
    jar 不持久化 http cookie——后续认证请求显式携带，T10 house pattern）。"""
    header = next(c for c in response.headers.get_list("set-cookie") if c.startswith("ac_session="))
    return header.split("=", 1)[1].split(";", 1)[0]


# ---------- request：非 active 拒绝矩阵 ----------


async def test_request_rejects_non_active_matrix(pg, flow_env):
    """仅 active 可发起注销：pending 经 HTTP 409 ACCOUNT_PENDING；suspended/deleting
    经服务层状态门 403/409（HTTP 面 get_v2_auth 先行拦截/会话已失效，A10）；deleted
    已不可认证，服务层兜底同 deleting 409。"""
    async with _auth_client(
        pg, flow_env, email="req-pending@example.com", status="pending", ip="10.0.60.1"
    ) as (client, csrf, uid, _tok):
        resp = await client.post(
            _ROUTE_REQUEST,
            json={"password": "pw-1"},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "req-pending"},
        )
    assert resp.status_code == 409
    assert resp.json() == _PENDING_409
    assert await _count(pg, "users", "id = :u AND status = 'pending'", {"u": uid}) == 1
    assert await _count(pg, "idempotency_records") == 0

    for status, code, http_status in (
        ("suspended", ErrorCode.ACCOUNT_SUSPENDED, 403),
        ("deleting", ErrorCode.ACCOUNT_DELETING, 409),
        ("deleted", ErrorCode.ACCOUNT_DELETING, 409),
    ):
        with pytest.raises(AgentCraftError) as exc_info:
            await deletion_service.request_deletion(
                flow_env,
                user_id="00000000-0000-0000-0000-000000000000",
                status=status,
                password_hash="h",
                mfa_secret_enc=None,
                email="x@example.com",
                password="pw",
                totp_code=None,
                idem_key=f"gate-{status}",
                idem_hash="0" * 64,
            )
        assert exc_info.value.code == code, status
        assert exc_info.value.http_status == http_status, status


# ---------- request：再认证失败（A10 口径）----------


async def test_request_wrong_password_401(pg, flow_env):
    """错密码 → 401 INVALID_CREDENTIALS「当前密码不正确」（与改密端点统一）；
    状态不动、会话存活、零业务写入。"""
    async with _auth_client(
        pg, flow_env, email="req-wrongpw@example.com", password="right-pw-1", ip="10.0.60.2"
    ) as (client, csrf, uid, session_token):
        resp = await client.post(
            _ROUTE_REQUEST,
            json={"password": "wrong-pw"},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "req-wrongpw"},
        )
    assert resp.status_code == 401
    assert resp.json() == _INVALID_CURRENT_401
    assert await _count(pg, "users", "id = :u AND status = 'active'", {"u": uid}) == 1
    assert not await _session_revoked(pg, session_token)
    assert await _count(pg, "account_action_tokens") == 0
    assert await _count(pg, "email_outbox") == 0
    assert await _count(pg, "idempotency_records") == 0


async def test_request_totp_gate_a15_ordering(pg, flow_env):
    """TOTP 已启用：错码/缺码 400 MFA_INVALID（计入 password_change_totp 窗口）；
    A15 排序钉死：错 TOTP + 错密码 → 400 MFA_INVALID 而非 401（先于 Argon2）。"""
    secret = pyotp.random_base32()
    async with _auth_client(
        pg,
        flow_env,
        email="req-totp@example.com",
        password="right-pw-1",
        totp_secret=secret,
        ip="10.0.60.3",
    ) as (client, csrf, uid, session_token):
        wrong = _wrong_code(secret)
        bodies = []
        for i, payload in enumerate(
            [
                {"password": "right-pw-1", "totp_code": wrong},
                {"password": "deliberately-wrong"},  # 缺码（A15：先于密码校验）
                {"password": "deliberately-wrong", "totp_code": wrong},
            ]
        ):
            resp = await client.post(
                _ROUTE_REQUEST,
                json=payload,
                headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"req-totp-{i}"},
            )
            assert resp.status_code == 400, i
            assert resp.json() == _MFA_INVALID_400, i
            bodies.append(resp.content)
        assert len(set(bodies)) == 1  # 失败形态响应体逐字节一致

    n = await _count(
        pg,
        "rate_limit_events",
        "scope = 'password_change_totp' AND subject_hash = :h",
        {"h": hmac_subject("user", uid)},
    )
    assert n == 3  # 缺码同样计入窗口（T12 同裁决）
    assert await _count(pg, "users", "id = :u AND status = 'active'", {"u": uid}) == 1
    assert not await _session_revoked(pg, session_token)


async def test_request_with_totp_correct_code_success(pg, flow_env):
    """TOTP + 当前密码均正确 → 200 注销成功；状态 deleting、全会话失效；无失败限流。"""
    secret = pyotp.random_base32()
    async with _auth_client(
        pg,
        flow_env,
        email="req-totp-ok@example.com",
        password="right-pw-1",
        totp_secret=secret,
        ip="10.0.60.4",
    ) as (client, csrf, uid, current_token):
        resp = await client.post(
            _ROUTE_REQUEST,
            json={"password": "right-pw-1", "totp_code": _current_code(secret)},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "req-totp-ok"},
        )
    assert resp.status_code == 200
    assert resp.json() == _REQUEST_200
    assert await _count(pg, "users", "id = :u AND status = 'deleting'", {"u": uid}) == 1
    assert await _session_revoked(pg, current_token)
    assert await _count(pg, "rate_limit_events", "scope = 'password_change_totp'") == 0


# ---------- request：成功全链 ----------


async def test_request_success_full_chain(pg, flow_env, monkeypatch):
    """成功注销：status deleting + deadline ~14d（DB 时钟差值）；全会话失效（含当前）；
    TERMINATE_TASKS_HOOK 收到 (db, user_id)；cancel 令牌 14d + outbox 同事务落行；
    幂等记录 200；响应清除双 cookie。"""
    hook_calls: list[str] = []

    async def hook(db, user_id):
        hook_calls.append(str(user_id))

    monkeypatch.setattr(deletion_service, "TERMINATE_TASKS_HOOK", hook)
    async with _auth_client(
        pg, flow_env, email="req-ok@example.com", password="right-pw-1", ip="10.0.60.5"
    ) as (client, csrf, uid, current_token):
        other_token, _ = await _extra_session(flow_env, uid, device="AgentCraft-Other/1.0")
        resp = await client.post(
            _ROUTE_REQUEST,
            json={"password": "right-pw-1"},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "req-ok"},
        )

    assert resp.status_code == 200
    assert resp.json() == _REQUEST_200
    assert hook_calls == [uid]
    assert len(resp.headers.get_list("set-cookie")) == 2  # 双 cookie 一并清除（A10）

    u = await _one(
        pg,
        "SELECT status, deletion_deadline_at, now() AS db_now FROM users WHERE id = :u",
        {"u": uid},
    )
    assert u["status"] == "deleting"
    assert (
        timedelta(days=13, hours=23) < u["deletion_deadline_at"] - u["db_now"] <= timedelta(days=14)
    )

    for tok in (current_token, other_token):
        assert await _session_revoked(pg, tok)

    rows = await _all(
        pg,
        "SELECT token_hash, consumed_at, expires_at, now() AS db_now FROM account_action_tokens "
        "WHERE user_id = :u AND purpose = 'deletion_cancel'",
        {"u": uid},
    )
    assert len(rows) == 1 and rows[0]["consumed_at"] is None
    assert (
        timedelta(days=13, hours=23)
        < rows[0]["expires_at"] - rows[0]["db_now"]
        <= timedelta(days=14)
    )

    ob = await _one(pg, "SELECT purpose, state FROM email_outbox WHERE user_id = :u", {"u": uid})
    assert ob["purpose"] == "deletion_cancel" and ob["state"] == "pending"

    rec = await _one(pg, "SELECT status_code, response_json FROM idempotency_records")
    assert rec["status_code"] == 200 and rec["response_json"] == _REQUEST_200


async def test_request_enqueue_failure_rolls_back(pg, flow_env, monkeypatch):
    """outbox 入队异常 → owner 事务整体回滚：绝不进入 deleting、无令牌、无 outbox 行、
    无幂等记录、会话全部存活（transactional outbox 原子性）。

    服务层直调注入（HTTP 面 500 形态属 main.py 通用兜底，非本契约——Starlette
    ServerErrorMiddleware 回传异常至 ASGI 客户端，HTTP 层断言不可靠）。"""

    async def boom(*args, **kwargs):
        raise RuntimeError("outbox down")

    uid = await _seed_user(pg, "req-rollback@example.com", password="right-pw-1")
    async with owner_session(flow_env, uid) as db:
        session_token, _csrf = await create_session(db, user_id=uuid.UUID(uid), device_label=_UA)
    urow = await _one(pg, "SELECT password_hash FROM users WHERE id = :u", {"u": uid})

    monkeypatch.setattr(deletion_service, "enqueue", boom)
    with pytest.raises(RuntimeError, match="outbox down"):
        await deletion_service.request_deletion(
            flow_env,
            user_id=uid,
            status="active",
            password_hash=urow["password_hash"],
            mfa_secret_enc=None,
            email="req-rollback@example.com",
            password="right-pw-1",
            totp_code=None,
            idem_key="req-rollback",
            idem_hash="0" * 64,
        )

    assert await _count(pg, "users", "id = :u AND status = 'active'", {"u": uid}) == 1
    assert await _count(pg, "account_action_tokens") == 0
    assert await _count(pg, "email_outbox") == 0
    assert await _count(pg, "idempotency_records") == 0
    assert not await _session_revoked(pg, session_token)


async def test_request_replay_same_key_beats_state_after_cancel(pg, flow_env):
    """同 key 重放先于状态检查（§7）：cancel 恢复 active 后重放原 200，业务零副作用
    （不新建令牌/outbox、状态保持 active）；重放不携带 Set-Cookie。"""
    async with _auth_client(
        pg, flow_env, email="req-replay@example.com", password="right-pw-1", ip="10.0.60.7"
    ) as (client, csrf, uid, _tok):
        first = await client.post(
            _ROUTE_REQUEST,
            json={"password": "right-pw-1"},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "req-replay"},
        )
        assert first.status_code == 200

    cancel_token = await _cancel_token_from_outbox(pg, uid)
    async with http_client("10.0.60.8") as pub:
        cancel = await pub.post(
            _ROUTE_CANCEL,
            json={"cancel_token": cancel_token, "password": "right-pw-1"},
            headers={"Idempotency-Key": "cancel-1"},
        )
        assert cancel.status_code == 200
    new_session = _session_cookie_from(cancel)  # cancel 产出全新会话（A12）
    async with http_client("10.0.60.8", cookies={"ac_session": new_session}) as authed:
        replay = await authed.post(
            _ROUTE_REQUEST,
            json={"password": "right-pw-1"},  # 新会话 + CSRF
            headers={
                "X-CSRF-Token": cancel.json()["data"]["csrf_token"],
                "Idempotency-Key": "req-replay",
            },
        )

    assert replay.status_code == 200
    assert replay.json() == _REQUEST_200  # 原样回放（status 仍 deleting 的旧响应）
    assert replay.headers.get_list("set-cookie") == []  # 重放不携带 Set-Cookie
    assert await _count(pg, "users", "id = :u AND status = 'active'", {"u": uid}) == 1
    assert await _count(pg, "account_action_tokens", "user_id = :u", {"u": uid}) == 1
    assert await _count(pg, "email_outbox", "user_id = :u", {"u": uid}) == 1
    assert await _count(pg, "idempotency_records") == 2  # request + cancel 各一


# ---------- cancel：成功 ----------


async def test_cancel_success_restores_active_with_new_session(pg, flow_env):
    """cancel：恢复 active + deadline 置空 + 全新会话（双 cookie）+ 令牌恰消费；
    同 key 重放原 200 且不重复建会话（A12）；新会话立即可用（GET status 200）。"""
    async with _auth_client(
        pg, flow_env, email="cancel-ok@example.com", password="right-pw-1", ip="10.0.60.9"
    ) as (client, csrf, uid, old_token):
        req = await client.post(
            _ROUTE_REQUEST,
            json={"password": "right-pw-1"},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": "cancel-ok"},
        )
        assert req.status_code == 200
        assert await _session_revoked(pg, old_token)

    cancel_token = await _cancel_token_from_outbox(pg, uid)
    async with http_client("10.0.60.10") as pub:
        body = {"cancel_token": cancel_token, "password": "right-pw-1"}
        first = await pub.post(_ROUTE_CANCEL, json=body, headers={"Idempotency-Key": "cancel-ok-1"})
        assert first.status_code == 200
        data = first.json()["data"]
        assert data["user"] == {
            "id": uid,
            "email": "cancel-ok@example.com",
            "role": "user",
            "status": "active",
        }
        assert data["csrf_token"]
        assert len(first.headers.get_list("set-cookie")) == 2  # 双 cookie 种入（A12/A14）

        replay = await pub.post(
            _ROUTE_CANCEL, json=body, headers={"Idempotency-Key": "cancel-ok-1"}
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert replay.headers.get_list("set-cookie") == []  # 重放不种 cookie

    new_session = _session_cookie_from(first)
    async with http_client("10.0.60.10", cookies={"ac_session": new_session}) as authed:
        status = await authed.get(_ROUTE_STATUS)  # 新会话立即可用（GET 免 CSRF）

    assert status.status_code == 200
    assert status.json() == _STATUS_200
    assert (
        await _count(
            pg,
            "users",
            "id = :u AND status = 'active' AND deletion_deadline_at IS NULL",
            {"u": uid},
        )
        == 1
    )
    row = await _one(
        pg, "SELECT consumed_at FROM account_action_tokens WHERE user_id = :u", {"u": uid}
    )
    assert row["consumed_at"] is not None
    assert await _count(pg, "sessions", "user_id = :u AND revoked_at IS NULL", {"u": uid}) == 1


# ---------- cancel：失效形态统一 409 + 分桶限流 ----------


async def test_cancel_failure_modes_uniform_409_byte_identical(pg, flow_env):
    """过期/已消费/非 deleting/未知令牌统一 409「撤销链接无效或已过期」（响应体
    逐字节一致，防探测）；失败路径零副作用（令牌不消费、状态不动、无会话）。"""
    expired_uid = await _seed_user(pg, "c-exp@example.com", password="pw-1", status="deleting")
    expired_token = await _seed_reset_token(
        pg, expired_uid, purpose="deletion_cancel", expired=True
    )
    consumed_uid = await _seed_user(pg, "c-used@example.com", password="pw-2", status="deleting")
    used_token = await _seed_reset_token(pg, consumed_uid, purpose="deletion_cancel", consumed=True)
    wrong_status_uid = await _seed_user(
        pg, "c-active@example.com", password="pw-3", status="active"
    )
    wrong_status_token = await _seed_reset_token(pg, wrong_status_uid, purpose="deletion_cancel")

    requests = [
        ("expired", {"cancel_token": expired_token, "password": "pw-1"}),
        ("consumed", {"cancel_token": used_token, "password": "pw-2"}),
        ("wrong_status", {"cancel_token": wrong_status_token, "password": "pw-3"}),
        ("unknown", {"cancel_token": generate_token(), "password": "pw-4"}),
    ]
    bodies = []
    async with http_client("10.0.60.11") as pub:
        for i, (mode, payload) in enumerate(requests):
            resp = await pub.post(
                _ROUTE_CANCEL, json=payload, headers={"Idempotency-Key": f"c-uniform-{i}"}
            )
            assert resp.status_code == 409, mode
            assert resp.json() == _INVALID_CANCEL_409, mode
            bodies.append(resp.content)
    assert len(set(bodies)) == 1  # 全部失效形态响应体逐字节一致（防探测）

    assert await _count(pg, "account_action_tokens", "consumed_at IS NULL") == 2  # 分文不消费
    assert await _count(pg, "users", "id = :u AND status = 'deleting'", {"u": expired_uid}) == 1
    assert await _count(pg, "users", "id = :u AND status = 'deleting'", {"u": consumed_uid}) == 1
    assert await _count(pg, "users", "id = :u AND status = 'active'", {"u": wrong_status_uid}) == 1
    assert await _count(pg, "sessions") == 0
    assert await _count(pg, "idempotency_records") == 0


async def test_cancel_wrong_password_401_token_not_consumed(pg, flow_env):
    """cancel 唯一用户侧凭据路径（review round 1 补测）：错密码 → 401
    INVALID_CREDENTIALS「当前密码不正确」（与改密端点统一文案；凭据校验非防探测
    桶，不要求与 409 形态逐字节一致）。owner 事务回滚：令牌分文不消费、状态仍
    deleting、无会话；幂等零记录（401 先于幂等 store——begin 未命中即零写入，
    失败路径不写幂等记录，与 T10/T12 confirm 同款纪律）；attempt 桶照常计数
    （合法令牌路径的尝试，enforce 自管事务独立提交不随业务回滚）。"""
    uid = await _seed_user(pg, "c-wrongpw@example.com", password="right-pw-1", status="deleting")
    token = await _seed_reset_token(pg, uid, purpose="deletion_cancel")
    async with http_client("10.0.60.18") as pub:
        resp = await pub.post(
            _ROUTE_CANCEL,
            json={"cancel_token": token, "password": "deliberately-wrong"},
            headers={"Idempotency-Key": "c-wrongpw-1"},
        )

    assert resp.status_code == 401
    assert resp.json() == _INVALID_CURRENT_401
    row = await _one(
        pg, "SELECT consumed_at FROM account_action_tokens WHERE user_id = :u", {"u": uid}
    )
    assert row["consumed_at"] is None  # 事务回滚，令牌分文不消费
    assert await _count(pg, "users", "id = :u AND status = 'deleting'", {"u": uid}) == 1
    assert await _count(pg, "sessions") == 0  # 未建新会话
    assert await _count(pg, "idempotency_records") == 0  # 401 先于幂等 store → 零幂等行
    # 合法令牌路径的尝试照常计入 attempt 桶（user/ip 两主体各 1 行）；探测桶零计数
    for kind, value in (("user", uid), ("ip", "10.0.60.18")):
        n = await _count(
            pg,
            "rate_limit_events",
            "scope = 'deletion_cancel_attempt' AND subject_hash = :h",
            {"h": hmac_subject(kind, value)},
        )
        assert n == 1, kind
    assert await _count(pg, "rate_limit_events", "scope = 'deletion_cancel_invalid'") == 0


async def test_cancel_forged_token_flood_429_ip_bucket(pg, flow_env):
    """伪造令牌探测：deletion_cancel_invalid 10/h [HMAC(ip)]，第 11 次 429 +
    Retry-After；拒绝路径不写事件；attempt 桶零计数（分桶纪律）。"""
    async with http_client("10.0.60.12") as pub:
        for i in range(10):
            resp = await pub.post(
                _ROUTE_CANCEL,
                json={"cancel_token": generate_token(), "password": "x"},
                headers={"Idempotency-Key": f"flood-{i}"},
            )
            assert resp.status_code == 409, i
        eleventh = await pub.post(
            _ROUTE_CANCEL,
            json={"cancel_token": generate_token(), "password": "x"},
            headers={"Idempotency-Key": "flood-10"},
        )

    assert eleventh.status_code == 429
    assert eleventh.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    assert 1 <= int(eleventh.headers["Retry-After"]) <= 3600
    assert await _count(pg, "rate_limit_events", "scope = 'deletion_cancel_invalid'") == 10
    assert await _count(pg, "rate_limit_events", "scope = 'deletion_cancel_attempt'") == 0
    assert await _count(pg, "idempotency_records") == 0


async def test_cancel_valid_path_11th_attempt_429(pg, flow_env):
    """合法路径（令牌存在）计 deletion_cancel_attempt [user, ip]：第 1 次 200、
    第 2-10 次 409（令牌已消费），第 11 次 429；invalid 桶零计数。"""
    uid = await _seed_user(pg, "c-rl@example.com", password="right-pw-1", status="deleting")
    token = await _seed_reset_token(pg, uid, purpose="deletion_cancel")
    async with http_client("10.0.60.13") as pub:
        first = await pub.post(
            _ROUTE_CANCEL,
            json={"cancel_token": token, "password": "right-pw-1"},
            headers={"Idempotency-Key": "c-rl-0"},
        )
        assert first.status_code == 200
        for i in range(1, 10):
            resp = await pub.post(
                _ROUTE_CANCEL,
                json={"cancel_token": token, "password": "right-pw-1"},
                headers={"Idempotency-Key": f"c-rl-{i}"},
            )
            assert resp.status_code == 409, i
        eleventh = await pub.post(
            _ROUTE_CANCEL,
            json={"cancel_token": token, "password": "right-pw-1"},
            headers={"Idempotency-Key": "c-rl-10"},
        )

    assert eleventh.status_code == 429
    assert eleventh.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    # 组合主体逐 subject 各写一行（T11 语义）：user/ip 两桶各恰 10 行
    for kind, value in (("user", uid), ("ip", "10.0.60.13")):
        n = await _count(
            pg,
            "rate_limit_events",
            "scope = 'deletion_cancel_attempt' AND subject_hash = :h",
            {"h": hmac_subject(kind, value)},
        )
        assert n == 10, kind
    assert await _count(pg, "rate_limit_events", "scope = 'deletion_cancel_invalid'") == 0


# ---------- status：形状钉死 + A10 语义 ----------


async def test_status_endpoint_shapes(pg, flow_env):
    """GET status：active 用户 200 形状钉死（days_remaining/deadline_at 均 null）；
    deleting 用户因全会话失效 401（A10 语义）；视图函数 deleting 形状直接钉死。"""
    async with _auth_client(pg, flow_env, email="status-active@example.com", ip="10.0.60.14") as (
        client,
        _csrf,
        uid,
        _tok,
    ):
        resp = await client.get(_ROUTE_STATUS)
    assert resp.status_code == 200
    assert resp.json() == _STATUS_200
    assert await _count(pg, "users", "id = :u AND status = 'active'", {"u": uid}) == 1

    view = deletion_service.deletion_status_view(
        status="deleting", deadline_at=datetime.now(timezone.utc) + timedelta(days=3, hours=12)
    )
    assert view["status"] == "deleting"
    assert view["days_remaining"] == 4  # ceil(3.5)
    assert view["deadline_at"] is not None

    await _seed_user(pg, "status-del@example.com", status="deleting")
    async with http_client("10.0.60.15") as pub:
        denied = await pub.get(_ROUTE_STATUS)  # deleting 用户无会话 → 401（A10）
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "SESSION_EXPIRED"


# ---------- sweep：到期清理 + 匿名化 + 审计保留 ----------


async def test_sweep_expired_anonymizes_and_preserves_audit(pg, flow_env):
    """到期 deleting → deleted + 匿名化（email 换 deleted+<id>@users.invalid、mfa
    清空、密码不可验）+ deadline/deleted_at 收口 + 全会话失效；审计行保留（users
    行不删除，actor_id FK 引用存活）；未到期 deleting 不处理。"""
    expired_uid = await _seed_deleting_user(
        pg,
        "sweep-exp@example.com",
        password="sweep-pw-1",
        expired=True,
        totp_secret=pyotp.random_base32(),
    )
    fresh_uid = await _seed_deleting_user(
        pg, "sweep-fresh@example.com", password="sweep-pw-2", expired=False
    )
    audit_id = await _seed_audit_log(pg, expired_uid)
    async with owner_session(flow_env, expired_uid) as db:
        await create_session(db, user_id=uuid.UUID(expired_uid), device_label=_UA)

    swept = await deletion_service.sweep_expired(flow_env)

    assert swept == 1
    row = await _one(
        pg,
        "SELECT status, deleted_at, deletion_deadline_at, email, mfa_secret_enc, password_hash "
        "FROM users WHERE id = :u",
        {"u": expired_uid},
    )
    assert row["status"] == "deleted" and row["deleted_at"] is not None
    assert row["deletion_deadline_at"] is None
    assert row["email"] == f"deleted+{expired_uid}@users.invalid"  # id 内嵌，唯一约束满足
    assert row["mfa_secret_enc"] is None
    assert verify_password("sweep-pw-1", row["password_hash"]) is False  # 凭据全灭
    assert (
        await _count(pg, "sessions", "user_id = :u AND revoked_at IS NOT NULL", {"u": expired_uid})
        == 1
    )

    untouched = await _one(pg, "SELECT status, email FROM users WHERE id = :u", {"u": fresh_uid})
    assert untouched["status"] == "deleting" and untouched["email"] == "sweep-fresh@example.com"

    audit = await _one(pg, "SELECT actor_id FROM audit_logs WHERE id = :i", {"i": audit_id})
    assert str(audit["actor_id"]) == expired_uid  # 审计引用保留


# ---------- A5：Idempotency-Key 必带 + schema 边界 ----------


async def test_deletion_endpoints_require_idempotency_key(pg, flow_env):
    """request/cancel 均必带 Idempotency-Key（A5）：缺失 400 VALIDATION_ERROR，零
    副作用；超长 cancel_token（257 > 256）schema 门 400 零 DB 副作用。"""
    async with _auth_client(
        pg, flow_env, email="req-nokey@example.com", password="right-pw-1", ip="10.0.60.16"
    ) as (client, csrf, uid, session_token):
        resp = await client.post(
            _ROUTE_REQUEST, json={"password": "right-pw-1"}, headers={"X-CSRF-Token": csrf}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert not await _session_revoked(pg, session_token)
        assert await _count(pg, "users", "id = :u AND status = 'active'", {"u": uid}) == 1

    async with http_client("10.0.60.17") as pub:
        no_key = await pub.post(_ROUTE_CANCEL, json={"cancel_token": "x", "password": "y"})
        big = await pub.post(
            _ROUTE_CANCEL,
            json={"cancel_token": "t" * 257, "password": "y"},
            headers={"Idempotency-Key": "bounds"},
        )

    assert no_key.status_code == 400 and no_key.json()["error"]["code"] == "VALIDATION_ERROR"
    assert big.status_code == 400 and big.json()["error"]["code"] == "VALIDATION_ERROR"
    assert await _count(pg, "idempotency_records") == 0
    assert await _count(pg, "rate_limit_events") == 0  # schema/依赖门先于限流与业务
