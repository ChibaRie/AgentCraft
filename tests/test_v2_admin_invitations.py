"""admin 邀请管理端到端测试（Phase 7 T2；Sup §6:132-134）。

- HTTP 层经 httpx.AsyncClient(ASGITransport) 驱动真实 app（admin_client 直驱全局
  app；ASGITransport 跑在测试自身循环，无 TestClient 跨循环坑）；
- 种子/复核一律 superuser（pg.engine）：invitations 无 RLS 但 app role 零 INSERT；
  email_outbox 断言走 admin role 真实写入路径的产物（0009 email_outbox_admin_insert
  生效实证——admin 事务内落 user_id=NULL 行，非 superuser 直插）；
- 密钥注入：admin_env 三件套 + EMAIL_OUTBOX_ENCRYPTION_KEY（创建路径 outbox 信封
  加密/测试解密同材料；Settings() 每次现读，monkeypatch 用例间互不污染）；
- token 红线断言（Sup:132）：明文 token 只从 outbox payload 解出（明文-哈希对应），
  响应体与审计行零 token。
"""

import json
import re
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from backend.main import app
from backend.utils.crypto import decrypt_text, make_keyring
from backend.v2.ids import uuid7
from backend.v2.runtime import owner_session
from backend.v2.security import generate_token, hash_token
from backend.v2.session_service import COOKIE_NAME, create_session
from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import _KEY_MATERIAL, admin_client

# admin_env 夹具发现形态：pytest 经模块命名空间发现夹具，「import + 参数同名」
# 触发 ruff F811——以赋值别名引入（T1 钉死形态，见 test_v2_admin_deps.py:48-50）。
admin_env = _vah.admin_env

_UA = "AgentCraft-AdminInvTest/1.0"
_CREATE = "/api/admin/invitations"


@pytest.fixture
async def outbox_env(admin_env, monkeypatch):
    """admin_env + EMAIL_OUTBOX_ENCRYPTION_KEY（创建路径 outbox 信封加密所需）。"""
    monkeypatch.setenv("EMAIL_OUTBOX_ENCRYPTION_KEY", _KEY_MATERIAL)
    yield admin_env


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


async def _seed_invitation(
    pg,
    email: str,
    *,
    consumed: bool = False,
    revoked: bool = False,
    expired: bool = False,
    created_at: datetime | None = None,
) -> tuple[str, str]:
    """播种邀请行，返回 (invitation id, 明文 token)。app role 无 invitations INSERT。"""
    token = generate_token()
    iid = str(uuid7())
    now = created_at or datetime.now(timezone.utc)
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO invitations (id, token_hash, email, expires_at, consumed_at, "
                "revoked_at, created_at) VALUES (:i, :t, :e, :exp, :c, :r, :ca)"
            ),
            {
                "i": iid,
                "t": hash_token(token),
                "e": email,
                "exp": now - timedelta(hours=1) if expired else now + timedelta(days=7),
                "c": now if consumed else None,
                "r": now if revoked else None,
                "ca": now,
            },
        )
    return iid, token


async def _one(pg, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


async def _count(pg, table: str, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {})
        ).scalar_one()


def _decrypt_outbox_payload(row: dict) -> dict:
    """以行 id 重建 AAD 解密 outbox payload（密钥材料为测试注入值）。"""
    _, keyring = make_keyring(f"primary:{_KEY_MATERIAL}")
    return json.loads(
        decrypt_text(
            json.loads(row["payload_ciphertext"]),
            aad=f"agentcraft:email_outbox:{row['id']}:v1",
            keyring=keyring,
        )
    )


async def _user_client(pg, rt, *, email: str) -> httpx.AsyncClient:
    """role='user'（无 TOTP）+ mfa_verified 会话客户端——若 admin 门②③先行会误报
    ADMIN_MFA_REQUIRED，403 FORBIDDEN 即门序① role 门先行的证据。"""
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
            db, user_id=_uuid.UUID(uid), device_label=_UA, mfa_verified=True
        )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.7.0.9", 51001)),
        base_url="http://testserver",
        headers={"User-Agent": _UA, "X-CSRF-Token": csrf},
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


async def _create(
    client: httpx.AsyncClient,
    *,
    email: str = "invitee@example.com",
    days: int = 7,
    reason: str = "月度邀请",
    key: str = "k-create-1",
    body: dict | None = None,
) -> httpx.Response:
    payload = (
        body if body is not None else {"email": email, "expires_in_days": days, "reason": reason}
    )
    return await client.post(_CREATE, json=payload, headers={"Idempotency-Key": key})


async def _revoke(
    client: httpx.AsyncClient, iid: str, *, reason: str = "清理失效邀请", key: str = "k-revoke-1"
) -> httpx.Response:
    return await client.post(
        f"{_CREATE}/{iid}/revoke", json={"reason": reason}, headers={"Idempotency-Key": key}
    )


# ---------- 创建（POST /api/admin/invitations）----------


async def test_create_invitation_201_full_chain_and_token_red_lines(pg, outbox_env):
    """创建 201 全链：响应仅邀请元数据（Sup:132）；邀请行/outbox 行/审计行同事务
    落库（outbox 行为 admin role 真实写入——0009 email_outbox_admin_insert 生效
    实证）；明文 token 只从 outbox payload 解出且明文-哈希对应；响应与审计零 token。"""
    client, _csrf, admin_id = await admin_client(pg, outbox_env, email="creator@example.com")
    try:
        resp = await _create(client, email="Invitee@Example.COM", days=7, reason="内测邀请")
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert set(data) == {"id", "email", "expires_at"}  # 响应只含邀请元数据
        assert data["email"] == "invitee@example.com"  # 小写归一（accept 精确匹配同款）

        inv = await _one(
            pg,
            "SELECT id, token_hash, email, expires_at, consumed_at, revoked_at, created_by "
            "FROM invitations WHERE id = CAST(:i AS uuid)",
            {"i": data["id"]},
        )
        assert inv["email"] == "invitee@example.com"
        assert inv["consumed_at"] is None and inv["revoked_at"] is None
        assert re.fullmatch(r"[0-9a-f]{64}", inv["token_hash"])
        assert str(inv["created_by"]) == admin_id
        # DB 行 expires_at 为 datetime（超管复核）；API 面为 isoformat 字符串
        delta = inv["expires_at"] - datetime.now(timezone.utc)
        assert timedelta(days=6, hours=23) < delta < timedelta(days=7, minutes=1)

        # outbox 行真实落库（admin role 非超级用户事务插入成功 = 0009 授权生效实证）
        outbox = await _one(
            pg, "SELECT id, user_id, purpose, state, payload_ciphertext FROM email_outbox"
        )
        assert outbox["user_id"] is None  # 邀请场景 user_id=NULL（D4③ 形态）
        assert outbox["purpose"] == "invitation" and outbox["state"] == "pending"
        payload = _decrypt_outbox_payload(outbox)
        assert payload["template_id"] == "invitation"
        assert payload["recipient"] == "invitee@example.com"
        assert payload["vars"]["valid_hours"] == 168
        token = payload["vars"]["action_token"]
        assert hash_token(token) == inv["token_hash"]  # 明文-哈希对应（SHA-256 同款）

        # 响应与审计零 token（Sup:132 红线）
        assert token not in resp.text
        audit = await _one(
            pg, "SELECT action, actor_id, target_type, target_id, reason, detail FROM audit_logs"
        )
        assert audit["action"] == "invitation.create"
        assert audit["target_type"] == "invitation" and str(audit["target_id"]) == data["id"]
        assert audit["detail"] == {
            "invitation_id": data["id"],
            "email": "invitee@example.com",
            "expires_in_days": 7,
        }
        assert token not in json.dumps(audit["detail"]) and token not in audit["reason"]

        # 跨面闭环：admin 创建的邀请（明文 token 只经 outbox payload 投递）可被既有
        # owner 面接受流程消费——同款 hash_token/generate_token 原语 + 小写归一匹配
        accept = await client.post(
            "/api/v2/auth/invitations/accept",
            json={
                "invitation_token": token,
                "email": "invitee@example.com",
                "password": "pw-123456",
            },
            headers={"Idempotency-Key": "k-accept-1"},
        )
        assert accept.status_code == 200
        assert (await _one(pg, "SELECT status FROM users WHERE email = 'invitee@example.com'"))[
            "status"
        ] == "pending"
        consumed = await _one(
            pg,
            "SELECT consumed_at FROM invitations WHERE id = CAST(:i AS uuid)",
            {"i": data["id"]},
        )
        assert consumed["consumed_at"] is not None
    finally:
        await client.aclose()


async def test_create_invitation_validation_400s_zero_side_effects(pg, outbox_env):
    """>7 天 400（brief 钉死）；非正天数同罚；非法 email 400 VALIDATION_ERROR；
    校验失败零副作用（邀请/outbox/审计零行）。"""
    client, _csrf, _admin_id = await admin_client(pg, outbox_env, email="validator@example.com")
    try:
        for bad_days in (8, 0, -1):
            resp = await _create(client, days=bad_days, key=f"k-days-{bad_days}")
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        resp = await _create(client, email="not-an-email", key="k-bad-email")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await _count(pg, "invitations") == 0
        assert await _count(pg, "email_outbox") == 0
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()


async def test_create_invitation_duplicate_open_email_409_then_slot_release(pg, outbox_env):
    """重复 open email 409（invitations_one_open_email）；409 零副作用（outbox/审计
    零新增）；revoke 释放邮箱槽后同邮箱可再建（部分唯一索引语义）。"""
    client, _csrf, _admin_id = await admin_client(pg, outbox_env, email="dup-admin@example.com")
    try:
        first = await _create(client, email="dup@example.com", key="k-dup-1")
        assert first.status_code == 201
        dup = await _create(client, email="dup@example.com", key="k-dup-2")
        assert dup.status_code == 409
        assert dup.json()["error"]["code"] == "INVITATION_INVALID"
        assert await _count(pg, "invitations") == 1
        assert await _count(pg, "email_outbox") == 1  # 409 路径零 outbox/审计残留
        assert await _count(pg, "audit_logs") == 1

        iid = first.json()["data"]["id"]
        revoked = await _revoke(client, iid, key="k-dup-r1")
        assert revoked.status_code == 200
        again = await _create(client, email="dup@example.com", key="k-dup-3")
        assert again.status_code == 201  # 槽已释放（revoked 不在 open 谓词内）
        assert again.json()["data"]["id"] != iid
        assert await _count(pg, "invitations") == 2
        assert await _count(pg, "email_outbox") == 2
    finally:
        await client.aclose()


async def test_create_invitation_idempotent_replay_and_payload_conflict(pg, outbox_env):
    """同 key 同载荷原样重放（201 同响应体，零新副作用）；同 key 异载荷（reason
    参与 request_hash）→ 409 IDEMPOTENCY_CONFLICT。"""
    client, _csrf, _admin_id = await admin_client(pg, outbox_env, email="idem@example.com")
    try:
        first = await _create(client, email="idem@example.com", key="k-replay")
        assert first.status_code == 201
        replay = await _create(client, email="idem@example.com", key="k-replay")
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert await _count(pg, "invitations") == 1
        assert await _count(pg, "email_outbox") == 1
        assert await _count(pg, "audit_logs") == 1  # 重放零新副作用

        conflict = await _create(
            client, email="idem@example.com", key="k-replay", reason="另一理由"
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert await _count(pg, "invitations") == 1
    finally:
        await client.aclose()


# ---------- 列表（GET /api/admin/invitations）----------


async def test_list_invitations_filter_pagination_and_invalid_params(pg, admin_env):
    """四态判定（consumed/revoked 终态优先于 expired；open=未消费未撤销未过期）、
    状态过滤、created_at DESC 稳定序分页、词表外 status 与非法分页 400。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="lister@example.com")
    try:
        base = datetime.now(timezone.utc)
        seeds = {
            "open-a@example.com": await _seed_invitation(
                pg, "open-a@example.com", created_at=base - timedelta(hours=50)
            ),
            "consumed@example.com": await _seed_invitation(
                pg, "consumed@example.com", consumed=True, created_at=base - timedelta(hours=40)
            ),
            "expired@example.com": await _seed_invitation(
                pg, "expired@example.com", expired=True, created_at=base - timedelta(hours=30)
            ),
            "revoked@example.com": await _seed_invitation(
                pg, "revoked@example.com", revoked=True, created_at=base - timedelta(hours=20)
            ),
            "open-b@example.com": await _seed_invitation(
                pg, "open-b@example.com", created_at=base - timedelta(hours=10)
            ),
            "open-c@example.com": await _seed_invitation(
                pg, "open-c@example.com", created_at=base - timedelta(hours=5)
            ),
        }
        resp = await client.get(_CREATE)
        assert resp.status_code == 200
        page_data = resp.json()["data"]
        assert page_data["total"] == 6 and page_data["page"] == 1 and page_data["size"] == 20
        by_email = {item["email"]: item["status"] for item in page_data["items"]}
        assert by_email == {
            "open-a@example.com": "open",
            "consumed@example.com": "consumed",
            "expired@example.com": "expired",
            "revoked@example.com": "revoked",
            "open-b@example.com": "open",
            "open-c@example.com": "open",
        }
        item_keys = set(page_data["items"][0])
        assert item_keys == {
            "id",
            "email",
            "status",
            "created_at",
            "expires_at",
            "consumed_at",
            "revoked_at",
        }
        assert "token" not in json.dumps(page_data)  # 列表零 token/token_hash 面

        for status_value, emails in (
            ("open", ["open-a@example.com", "open-b@example.com", "open-c@example.com"]),
            ("consumed", ["consumed@example.com"]),
            ("expired", ["expired@example.com"]),
            ("revoked", ["revoked@example.com"]),
        ):
            filtered = (await client.get(_CREATE, params={"status": status_value})).json()["data"]
            assert filtered["total"] == len(emails)
            assert sorted(it["email"] for it in filtered["items"]) == sorted(emails)

        paged = (await client.get(_CREATE, params={"status": "open", "page": 1, "size": 2})).json()[
            "data"
        ]
        assert paged["total"] == 3
        assert [it["email"] for it in paged["items"]] == [
            "open-c@example.com",
            "open-b@example.com",
        ]
        paged2 = (
            await client.get(_CREATE, params={"status": "open", "page": 2, "size": 2})
        ).json()["data"]
        assert [it["email"] for it in paged2["items"]] == ["open-a@example.com"]
        assert seeds["open-a@example.com"][0] == paged2["items"][0]["id"]

        for params in ({"status": "bogus"}, {"page": 0}, {"size": 101}):
            bad = await client.get(_CREATE, params=params)
            assert bad.status_code == 400
            assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


# ---------- 撤销（POST /api/admin/invitations/{id}/revoke）----------


async def test_revoke_success_row_values_audit_no_token(pg, admin_env):
    """撤销成功：行值真实变化（revoked_at 非空、consumed_at 仍 NULL）；审计
    invitation.revoke（detail 三键）；响应与审计零 token。"""
    iid, token = await _seed_invitation(pg, "victim@example.com")
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="revoker@example.com")
    try:
        resp = await _revoke(client, iid, reason="嘉宾名单变更")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {"id", "email", "status", "revoked_at"}
        assert data["id"] == iid and data["status"] == "revoked"
        row = await _one(
            pg,
            "SELECT consumed_at, revoked_at FROM invitations WHERE id = CAST(:i AS uuid)",
            {"i": iid},
        )
        assert row["revoked_at"] is not None and row["consumed_at"] is None
        audit = await _one(
            pg,
            "SELECT action, actor_id, target_id, reason, detail FROM audit_logs "
            "WHERE action = 'invitation.revoke'",
        )
        assert str(audit["target_id"]) == iid
        assert str(audit["actor_id"]) == admin_id
        assert audit["detail"] == {
            "invitation_id": iid,
            "email": "victim@example.com",
            "was_expired": False,
        }
        assert token not in resp.text
        assert token not in json.dumps(audit["detail"]) and token not in audit["reason"]
    finally:
        await client.aclose()


async def test_revoke_expired_succeeds_and_frees_email_slot(pg, outbox_env):
    """T2 申报语义：过期未消费未撤销可撤销（部分唯一索引谓词不含 expires_at，
    过期行仍占邮箱槽——revoke 是释放该槽的唯一杠杆）；置 revoked 后同邮箱可再建。"""
    iid, _token = await _seed_invitation(pg, "stale@example.com", expired=True)
    client, _csrf, _admin_id = await admin_client(pg, outbox_env, email="stale-admin@example.com")
    try:
        resp = await _revoke(client, iid, key="k-stale-r")
        assert resp.status_code == 200
        audit = await _one(pg, "SELECT detail FROM audit_logs WHERE action = 'invitation.revoke'")
        assert audit["detail"]["was_expired"] is True
        recreate = await _create(client, email="stale@example.com", key="k-stale-c")
        assert recreate.status_code == 201  # 邮箱槽已释放
    finally:
        await client.aclose()


async def test_revoke_consumed_409_no_mutation(pg, admin_env):
    """consumed → 409 INVITATION_CONSUMED；行零突变（revoked_at 仍 NULL）。"""
    iid, _token = await _seed_invitation(pg, "eaten@example.com", consumed=True)
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="c-admin@example.com")
    try:
        resp = await _revoke(client, iid)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVITATION_CONSUMED"
        row = await _one(
            pg,
            "SELECT consumed_at, revoked_at FROM invitations WHERE id = CAST(:i AS uuid)",
            {"i": iid},
        )
        assert row["consumed_at"] is not None and row["revoked_at"] is None
    finally:
        await client.aclose()


async def test_revoke_already_revoked_409_missing_404_bad_uuid_400(pg, admin_env):
    """已撤销 → 409 INVITATION_INVALID；不存在 → 统一 404 NOT_FOUND；非 UUID
    路径参数 → 400 VALIDATION_ERROR。"""
    iid, _token = await _seed_invitation(pg, "gone@example.com", revoked=True)
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="g-admin@example.com")
    try:
        resp = await _revoke(client, iid)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVITATION_INVALID"
        missing = await _revoke(client, str(_uuid.uuid4()), key="k-miss")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        bad = await _revoke(client, "not-a-uuid", key="k-bad")
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


# ---------- 门与壳负例（三端点全挂三重门）----------


async def test_non_admin_403_forbidden_role_gate_first_no_side_effects(pg, admin_env):
    """非 admin（无 TOTP + MFA 会话齐全——403 只能来自①role 门，先于②③）→
    403 FORBIDDEN；读写双向受门；业务零副作用（门先于状态门与服务）。"""
    client = await _user_client(pg, admin_env, email="plain@example.com")
    try:
        post = await _create(client, email="victim@example.com")
        assert post.status_code == 403
        assert post.json()["error"]["code"] == "FORBIDDEN"
        get = await client.get(_CREATE)
        assert get.status_code == 403
        assert get.json()["error"]["code"] == "FORBIDDEN"
        assert await _count(pg, "invitations") == 0
        assert await _count(pg, "email_outbox") == 0
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()


async def test_admin_mfa_window_expired_403_all_three_endpoints(pg, admin_env):
    """12h 门过期（回拨 13h）→ 三端点（含 GET）全 403 ADMIN_MFA_REQUIRED——
    读写全受门（Sup:126），且门先于幂等（旧 key 不能绕过）。"""
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="stale-mfa@example.com")
    try:
        await _rewind_mfa_verified(pg, admin_id, hours=13)
        get = await client.get(_CREATE)
        assert get.status_code == 403
        assert get.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
        post = await _create(client, key="k-expired-create")
        assert post.status_code == 403
        assert post.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
        revoke = await _revoke(client, str(_uuid.uuid4()), key="k-expired-revoke")
        assert revoke.status_code == 403
        assert revoke.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
    finally:
        await client.aclose()


async def test_create_reason_and_idem_key_gates_400(pg, outbox_env):
    """reason 缺失/空白 → 400 ADMIN_REASON_REQUIRED（壳层门，先于幂等）；
    缺 Idempotency-Key → 400 VALIDATION_ERROR；零副作用。"""
    client, _csrf, _admin_id = await admin_client(pg, outbox_env, email="reason@example.com")
    try:
        missing = await client.post(
            _CREATE,
            json={"email": "r@example.com", "expires_in_days": 3},
            headers={"Idempotency-Key": "k-no-reason"},
        )
        assert missing.status_code == 400
        assert missing.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        blank = await _create(client, reason="   ", key="k-blank")
        assert blank.status_code == 400
        assert blank.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        no_key = await client.post(
            _CREATE, json={"email": "r@example.com", "expires_in_days": 3, "reason": "ok"}
        )
        assert no_key.status_code == 400
        assert no_key.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await _count(pg, "invitations") == 0
        assert await _count(pg, "email_outbox") == 0
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()
