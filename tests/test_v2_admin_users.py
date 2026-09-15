"""admin 用户管理端到端测试（Phase 7 T3a；Sup §6:135-136/139-140）。

- HTTP 层经 httpx.AsyncClient(ASGITransport) 驱动真实 app（T2 invitations 同款
  PG-httpx 形态；admin_client 直驱全局 app）；
- 种子/复核一律 superuser（pg.engine）：users/tasks 属 owner-RLS 且 admin 无
  INSERT，user_quotas 无 RLS 但种子走 superuser 直插统一口径；
- **作者面消费门联动**（brief 钉死断言项）采用「直调 authoring 服务」形态：
  grant 前后调用 author_service.create_entity，断言 403 FORBIDDEN → 成功创建
  的行为转变（revoke 后门重新关闭）——不 mock，真实走 author_service 门 SQL；
- detail 无 Key 红线（Sup:136）：播种 user_providers 密文金丝雀，断言密文不出现
  在详情响应（测试播种 provider 链即为此服务）。
"""

import json
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import text

from backend.main import app
from backend.v2.author_service import create_entity
from backend.v2.ids import uuid7
from backend.v2.runtime import owner_session
from backend.v2.session_service import COOKIE_NAME, create_session
from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import admin_client

# admin_env 夹具发现形态：pytest 经模块命名空间发现夹具，「import + 参数同名」
# 触发 ruff F811——以赋值别名引入（T1 钉死形态，见 test_v2_admin_invitations.py:34）。
admin_env = _vah.admin_env

_UA = "AgentCraft-AdminUsersTest/1.0"
_USERS = "/api/admin/users"
_CANARY_CIPHERTEXT = "CANARY-CIPHERTEXT-never-leak-0123456789"


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


async def _seed_user(
    pg,
    email: str,
    *,
    status: str = "active",
    role: str = "user",
    created_at: datetime | None = None,
) -> str:
    """播种普通用户行（app role 无 users INSERT），返回 user id 字符串。"""
    uid = str(_uuid.uuid4())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, "
                "mfa_secret_enc, created_at) VALUES (:i, :e, 'h', :r, :s, NULL, :ca)"
            ),
            {
                "i": uid,
                "e": email,
                "r": role,
                "s": status,
                "ca": created_at or datetime.now(timezone.utc),
            },
        )
    return uid


async def _seed_task_chain(pg, owner_id: str) -> dict:
    """播种最小任务前置链（experts/expert_revisions/user_providers），返回外键 id 集合。

    tasks 三外键（expert_revision_id RESTRICT / provider_id RESTRICT）必须先行落位；
    user_providers 密文用金丝雀字面（detail 无 Key 红线断言的泄漏探针）。
    """
    async with pg.engine.begin() as conn:
        catalog_id = (
            await conn.execute(text("SELECT id FROM provider_catalog LIMIT 1"))
        ).scalar_one()
        expert_id, rev_id, provider_id = str(uuid7()), str(uuid7()), str(uuid7())
        await conn.execute(
            text("INSERT INTO experts (id, owner_id, status) VALUES (:i, :o, 'draft')"),
            {"i": expert_id, "o": owner_id},
        )
        await conn.execute(
            text(
                "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                "content_json, content_sha256, status) "
                "VALUES (:i, :e, :o, 1, CAST(:cj AS jsonb), :sha, 'draft')"
            ),
            {
                "i": rev_id,
                "e": expert_id,
                "o": owner_id,
                "cj": json.dumps({"name": "seed-expert"}),
                "sha": "a" * 64,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO user_providers (id, user_id, catalog_id, model_id, "
                "key_ciphertext, dek_wrapped, key_last4, key_version, status, is_default) "
                "VALUES (:i, :o, :c, 'gpt-4o-mini', :kc, :dw, 'ab12', 1, 'active', false)"
            ),
            {
                "i": provider_id,
                "o": owner_id,
                "c": catalog_id,
                "kc": _CANARY_CIPHERTEXT,
                "dw": "dw",
            },
        )
        return {
            "expert_id": expert_id,
            "revision_id": rev_id,
            "provider_id": provider_id,
            "catalog_id": str(catalog_id),
        }


async def _seed_task(pg, owner_id: str, chain: dict, *, status: str = "completed") -> str:
    tid = str(uuid7())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, expert_revision_id, provider_id, "
                "provider_catalog_id, provider_model_id, provider_key_version, status, "
                "event_sequence) VALUES (:i, :o, :r, :p, :c, 'gpt-4o-mini', 1, :s, 0)"
            ),
            {
                "i": tid,
                "o": owner_id,
                "r": chain["revision_id"],
                "p": chain["provider_id"],
                "c": chain["catalog_id"],
                "s": status,
            },
        )
    return tid


async def _seed_quota(
    pg, user_id: str, daily: int, active: int, running: int, retained: int
) -> None:
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_quotas (user_id, max_daily_tasks, max_active_tasks, "
                "max_running_tasks, max_retained_storage_bytes) "
                "VALUES (CAST(:u AS uuid), :d, :a, :r, :s)"
            ),
            {"u": user_id, "d": daily, "a": active, "r": running, "s": retained},
        )


async def _seed_usage(pg, user_id: str, active: int, running: int, retained: int) -> None:
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_quota_usage (user_id, active_tasks, running_tasks, "
                "retained_storage_bytes) VALUES (CAST(:u AS uuid), :a, :r, :s)"
            ),
            {"u": user_id, "a": active, "r": running, "s": retained},
        )


async def _one(pg, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


async def _count(pg, table: str, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params or {})
        ).scalar_one()


async def _user_client(pg, rt, *, email: str) -> httpx.AsyncClient:
    """role='user'（无 TOTP）+ mfa_verified 会话客户端——403 FORBIDDEN 即门序①
    role 门先行的证据（同 test_v2_admin_invitations.py 形态）。"""
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
        transport=httpx.ASGITransport(app=app, client=("10.7.0.9", 51002)),
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


async def _authoring_gate_status(rt, uid: str, key: str) -> int:
    """直调 authoring 服务探测 expert_author 消费门：403（门闭）/200（门开）。"""
    try:
        await create_entity(
            rt,
            user_id=uid,
            target="experts",
            content={"name": f"gate-probe-{key}"},
            idem_key=f"gate-{key}",
            idem_hash="h-" + key,
        )
    except HTTPException as exc:
        return exc.status_code
    return 200


# ---------- 请求助手 ----------


async def _put_quotas(client, uid: str, *, body: dict | None = None, key: str = "k-q1"):
    payload = body if body is not None else {"max_daily_tasks": 10, "reason": "容量评估"}
    return await client.put(
        f"{_USERS}/{uid}/quotas", json=payload, headers={"Idempotency-Key": key}
    )


async def _grant(client, uid: str, *, kind: str = "expert_author", key: str = "k-g1"):
    return await client.post(
        f"{_USERS}/{uid}/entitlements",
        json={"kind": kind, "reason": "作者资格审核通过"},
        headers={"Idempotency-Key": key},
    )


async def _revoke(
    client,
    uid: str,
    *,
    kind: str = "expert_author",
    reason: str = "作者资格撤销",
    key: str = "k-r1",
):
    return await client.request(
        "DELETE",
        f"{_USERS}/{uid}/entitlements",
        json={"kind": kind, "reason": reason},
        headers={"Idempotency-Key": key},
    )


# ---------- 列表（GET /api/admin/users）----------


async def test_list_users_prefix_status_pagination_and_invalid_params(pg, admin_env):
    """email 前缀过滤（通配字符转义 + 大小写不敏感）、状态过滤、created_at DESC
    稳定序分页、词表外 status 与非法分页 400。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="lister@example.com")
    try:
        base = datetime.now(timezone.utc)
        u1 = await _seed_user(pg, "user2@example.com", created_at=base - timedelta(hours=30))
        u2 = await _seed_user(pg, "u_ser@example.com", created_at=base - timedelta(hours=20))
        await _seed_user(pg, "pxct@example.com", created_at=base - timedelta(hours=10))
        await _seed_user(
            pg, "pending@example.com", status="pending", created_at=base - timedelta(hours=5)
        )
        # 金丝雀：前缀里的 % 须为字面量——不该匹配 pxct@example.com
        pct = await _seed_user(pg, "p%ct@example.com", created_at=base - timedelta(hours=2))

        resp = await client.get(_USERS)
        assert resp.status_code == 200
        page_data = resp.json()["data"]
        assert page_data["size"] == 20 and page_data["page"] == 1
        assert page_data["total"] == 6  # admin 自身 + 5 种子（pg 夹具按测试隔离）
        item_keys = set(page_data["items"][0])
        assert item_keys == {"id", "email", "role", "status", "created_at"}

        # 前缀 + 转义：u_ 只命中 u_ser（未转义时 u_ 会误吞 user2）
        got = (await client.get(_USERS, params={"email_prefix": "u_"})).json()["data"]
        assert [it["id"] for it in got["items"]] == [u2]
        # % 字面：p% 只命中 p%ct（未转义时 p% 会误吞 pxct）
        got = (await client.get(_USERS, params={"email_prefix": "p%"})).json()["data"]
        assert [it["id"] for it in got["items"]] == [pct]
        # 大小写不敏感前缀
        got = (await client.get(_USERS, params={"email_prefix": "USER"})).json()["data"]
        assert u1 in [it["id"] for it in got["items"]] and u2 not in [
            it["id"] for it in got["items"]
        ]

        # 状态过滤
        got = (await client.get(_USERS, params={"status": "pending"})).json()["data"]
        assert got["total"] == 1 and got["items"][0]["email"] == "pending@example.com"
        got = (
            await client.get(_USERS, params={"email_prefix": "pending@", "status": "pending"})
        ).json()["data"]
        assert got["total"] == 1

        # created_at DESC 稳定序分页（prefix=u 圈定 2 行：user2/u_ser，不含 pxct/p%ct）
        p1 = (await client.get(_USERS, params={"email_prefix": "u", "page": 1, "size": 1})).json()[
            "data"
        ]
        p2 = (await client.get(_USERS, params={"email_prefix": "u", "page": 2, "size": 1})).json()[
            "data"
        ]
        assert p1["total"] == 2 and [it["id"] for it in p1["items"]] == [
            u2
        ]  # u2（20h）新于 u1（30h）
        assert [it["id"] for it in p2["items"]] == [u1]

        for params in ({"status": "bogus"}, {"page": 0}, {"size": 101}):
            bad = await client.get(_USERS, params=params)
            assert bad.status_code == 400
            assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


# ---------- 详情（GET /api/admin/users/{id}）----------


async def test_get_user_detail_rows_defaults_task_counts_and_no_key_material(pg, admin_env):
    """详情：有行用户（配额/用量/任务计数/密文金丝雀不泄漏）+ 无行用户（ORM 列
    默认 5/3/1/1GiB 兜底）；admin_read SELECT 可见全量 tasks 计数；零 Key 材料。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="detail@example.com")
    try:
        rich = await _seed_user(pg, "rich@example.com")
        chain = await _seed_task_chain(pg, rich)
        await _seed_task(pg, rich, chain, status="completed")
        await _seed_task(pg, rich, chain, status="completed")
        await _seed_task(pg, rich, chain, status="running")
        await _seed_quota(pg, rich, 10, 7, 2, 2048)
        await _seed_usage(pg, rich, 2, 1, 500)
        # 他人任务不计入（owner 维度计数）
        other = await _seed_user(pg, "other@example.com")
        await _seed_task(pg, other, await _seed_task_chain(pg, other))

        resp = await client.get(f"{_USERS}/{rich}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {"user", "quotas", "usage", "tasks"}
        assert data["user"]["id"] == rich and data["user"]["email"] == "rich@example.com"
        assert data["quotas"] == {
            "max_daily_tasks": 10,
            "max_active_tasks": 7,
            "max_running_tasks": 2,
            "max_retained_storage_bytes": 2048,
        }
        assert data["usage"] == {
            "active_tasks": 2,
            "running_tasks": 1,
            "retained_storage_bytes": 500,
        }
        assert data["tasks"] == {"total": 3, "by_status": {"completed": 2, "running": 1}}
        # Sup:136 红线：provider 密文金丝雀不得出现在响应任何角落
        assert _CANARY_CIPHERTEXT not in resp.text
        assert "key_ciphertext" not in resp.text and "dek_wrapped" not in resp.text

        # 无行用户：配额/用量取 ORM 列默认（5/3/1/1GiB 与 0/0/0），计数为空
        bare = await _seed_user(pg, "bare@example.com")
        data = (await client.get(f"{_USERS}/{bare}")).json()["data"]
        assert data["quotas"] == {
            "max_daily_tasks": 5,
            "max_active_tasks": 3,
            "max_running_tasks": 1,
            "max_retained_storage_bytes": 1_073_741_824,
        }
        assert data["usage"] == {
            "active_tasks": 0,
            "running_tasks": 0,
            "retained_storage_bytes": 0,
        }
        assert data["tasks"] == {"total": 0, "by_status": {}}
    finally:
        await client.aclose()


async def test_get_user_detail_missing_404_bad_uuid_400(pg, admin_env):
    """不存在 → 统一 404 NOT_FOUND；非 UUID 路径参数 → 400 VALIDATION_ERROR。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="d-miss@example.com")
    try:
        missing = await client.get(f"{_USERS}/{_uuid.uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        bad = await client.get(f"{_USERS}/not-a-uuid")
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


# ---------- 配额（PUT /api/admin/users/{id}/quotas）----------


async def test_quotas_upsert_creates_row_with_defaults_then_partial_update(pg, admin_env):
    """首 PUT 惰性物化建行（未提供维度取 ORM 列默认，全列显式赋值）；二 PUT 部分
    更新（已提供维度覆盖、未提供维度保留首刷值，非回退默认）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="quota@example.com")
    try:
        uid = await _seed_user(pg, "quotaee@example.com")
        assert await _count(pg, "user_quotas") == 0

        first = await _put_quotas(client, uid, body={"max_daily_tasks": 10, "reason": "首发"})
        assert first.status_code == 200
        assert first.json()["data"] == {
            "user_id": uid,
            "quotas": {
                "max_daily_tasks": 10,
                "max_active_tasks": 3,
                "max_running_tasks": 1,
                "max_retained_storage_bytes": 1_073_741_824,
            },
        }
        row = await _one(
            pg,
            "SELECT max_daily_tasks, max_active_tasks, max_running_tasks, "
            "max_retained_storage_bytes FROM user_quotas WHERE user_id = CAST(:u AS uuid)",
            {"u": uid},
        )
        assert row == {
            "max_daily_tasks": 10,
            "max_active_tasks": 3,
            "max_running_tasks": 1,
            "max_retained_storage_bytes": 1_073_741_824,
        }

        second = await _put_quotas(
            client,
            uid,
            body={"max_running_tasks": 4, "max_retained_storage_bytes": 99, "reason": "二刷"},
            key="k-q2",
        )
        assert second.status_code == 200
        assert second.json()["data"]["quotas"] == {
            "max_daily_tasks": 10,  # 保留首刷值（非回退默认 5）
            "max_active_tasks": 3,
            "max_running_tasks": 4,
            "max_retained_storage_bytes": 99,
        }
    finally:
        await client.aclose()


async def test_quotas_audit_before_after_snapshots(pg, admin_env):
    """既有行部分更新：审计 user.quotas.update，detail before/after 为全四维快照
    （before=行现值，after=合并结果）；actor/reason/target 落审计行。"""
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="q-audit@example.com")
    try:
        uid = await _seed_user(pg, "qa@example.com")
        await _seed_quota(pg, uid, 2, 2, 2, 100)
        resp = await _put_quotas(
            client,
            uid,
            body={"max_daily_tasks": 7, "max_retained_storage_bytes": 999, "reason": "扩容评估"},
            key="k-qa",
        )
        assert resp.status_code == 200
        audit = await _one(
            pg,
            "SELECT action, actor_id, target_type, target_id, reason, detail FROM audit_logs "
            "WHERE action = 'user.quotas.update'",
        )
        assert audit["actor_id"] is not None and str(audit["actor_id"]) == admin_id
        assert audit["target_type"] == "user" and str(audit["target_id"]) == uid
        assert audit["reason"] == "扩容评估"
        assert audit["detail"] == {
            "user_id": uid,
            "before": {
                "max_daily_tasks": 2,
                "max_active_tasks": 2,
                "max_running_tasks": 2,
                "max_retained_storage_bytes": 100,
            },
            "after": {
                "max_daily_tasks": 7,
                "max_active_tasks": 2,
                "max_running_tasks": 2,
                "max_retained_storage_bytes": 999,
            },
        }
    finally:
        await client.aclose()


async def test_quotas_validation_400s_and_missing_user_404(pg, admin_env):
    """负值/布尔/未知维度/空配额体 → 400 VALIDATION_ERROR；用户不存在 → 404；
    非 UUID 路径参数 → 400；校验失败零副作用（user_quotas/audit 零行）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="q-bad@example.com")
    try:
        uid = await _seed_user(pg, "qb@example.com")
        for body in (
            {"max_daily_tasks": -1, "reason": "r"},
            {"max_daily_tasks": True, "reason": "r"},
            {"bogus_dim": 1, "reason": "r"},
            {"reason": "无配额字段"},
        ):
            bad = await _put_quotas(
                client, uid, body=body, key=f"k-qb-{json.dumps(body, sort_keys=True)}"
            )
            assert bad.status_code == 400, body
            assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
        missing = await _put_quotas(client, str(_uuid.uuid4()), key="k-qb-miss")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        bad_uuid = await _put_quotas(client, "not-a-uuid", key="k-qb-bad")
        assert bad_uuid.status_code == 400
        assert bad_uuid.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await _count(pg, "user_quotas") == 0
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()


async def test_quotas_idempotent_replay_and_payload_conflict(pg, admin_env):
    """同 key 同载荷原样重放（200 同响应体，零新副作用）；同 key 异 reason →
    409 IDEMPOTENCY_CONFLICT（reason 参与哈希）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="q-idem@example.com")
    try:
        uid = await _seed_user(pg, "qi@example.com")
        first = await _put_quotas(client, uid, key="k-q-replay")
        assert first.status_code == 200
        replay = await _put_quotas(client, uid, key="k-q-replay")
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert await _count(pg, "audit_logs") == 1  # 重放零新副作用
        conflict = await _put_quotas(
            client, uid, body={"max_daily_tasks": 10, "reason": "另一理由"}, key="k-q-replay"
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert await _count(pg, "audit_logs") == 1
    finally:
        await client.aclose()


# ---------- entitlement（POST/DELETE /api/admin/users/{id}/entitlements）----------


async def test_grant_success_audit_and_authoring_gate_opens(pg, admin_env):
    """grant 201 + 行值真实变化（granted_by/revoked_at NULL）+ 审计
    user.entitlement.grant；作者面消费门联动：grant 前 create_entity 403
    FORBIDDEN，grant 后 200 成功创建（直调 author_service 形态申报）。"""
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="grant@example.com")
    try:
        uid = await _seed_user(pg, "author@example.com")
        assert await _authoring_gate_status(admin_env, uid, "pre-grant") == 403

        resp = await _grant(client, uid)
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert set(data) == {"user_id", "entitlement", "entitlement_id", "granted_at"}
        assert data["user_id"] == uid and data["entitlement"] == "expert_author"
        row = await _one(
            pg,
            "SELECT user_id, entitlement, granted_by, granted_at, revoked_at "
            "FROM user_entitlements WHERE id = CAST(:i AS uuid)",
            {"i": data["entitlement_id"]},
        )
        assert str(row["user_id"]) == uid and row["entitlement"] == "expert_author"
        assert str(row["granted_by"]) == admin_id and row["revoked_at"] is None
        audit = await _one(
            pg,
            "SELECT action, actor_id, target_id, reason, detail FROM audit_logs "
            "WHERE action = 'user.entitlement.grant'",
        )
        assert str(audit["actor_id"]) == admin_id and str(audit["target_id"]) == uid
        assert audit["detail"] == {
            "user_id": uid,
            "entitlement": "expert_author",
            "entitlement_id": data["entitlement_id"],
        }

        # 门开：create_entity 真实建实体（app role RLS 全链真实写入）
        assert await _authoring_gate_status(admin_env, uid, "post-grant") == 200
    finally:
        await client.aclose()


async def test_grant_duplicate_active_409_then_slot_freed_by_revoke(pg, admin_env):
    """one_active 冲突 → 409 ENTITLEMENT_ACTIVE（409 零审计残留）；revoke 释放
    one_active 槽后同 kind 可再授（硬删活跃行语义）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="dup-g@example.com")
    try:
        uid = await _seed_user(pg, "dup@example.com")
        first = await _grant(client, uid, key="k-dup-g1")
        assert first.status_code == 201
        again = await _grant(client, uid, key="k-dup-g2")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "ENTITLEMENT_ACTIVE"
        assert await _count(pg, "user_entitlements") == 1  # 409 零残留
        assert await _count(pg, "audit_logs", "action = 'user.entitlement.grant'") == 1

        revoked = await _revoke(client, uid, key="k-dup-r1")
        assert revoked.status_code == 200
        regrant = await _grant(client, uid, key="k-dup-g3")
        assert regrant.status_code == 201  # 槽已释放（硬删，非软标 revoked_at）
        assert regrant.json()["data"]["entitlement_id"] != first.json()["data"]["entitlement_id"]
    finally:
        await client.aclose()


async def test_revoke_success_row_deleted_audit_and_gate_closes(pg, admin_env):
    """revoke 硬删活跃行（行值真实消失，非软标）+ 审计 user.entitlement.revoke
    （detail 记被删行 id）；作者面消费门重新关闭（403）。"""
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="revoker@example.com")
    try:
        uid = await _seed_user(pg, "victim-author@example.com")
        granted = await _grant(client, uid, key="k-rv-g")
        ent_id = granted.json()["data"]["entitlement_id"]
        assert await _authoring_gate_status(admin_env, uid, "pre-revoke") == 200

        resp = await _revoke(client, uid)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {"user_id", "entitlement", "entitlement_id", "revoked_at"}
        assert data["entitlement_id"] == ent_id
        assert await _count(pg, "user_entitlements", "id = CAST(:i AS uuid)", {"i": ent_id}) == 0
        audit = await _one(
            pg,
            "SELECT action, actor_id, target_id, detail FROM audit_logs "
            "WHERE action = 'user.entitlement.revoke'",
        )
        assert str(audit["actor_id"]) == admin_id and str(audit["target_id"]) == uid
        assert audit["detail"] == {
            "user_id": uid,
            "entitlement": "expert_author",
            "entitlement_id": ent_id,
        }
        assert await _authoring_gate_status(admin_env, uid, "post-revoke") == 403
    finally:
        await client.aclose()


async def test_revoke_missing_active_404_and_bad_kind_400(pg, admin_env):
    """无活跃行（从未授予 / 已撤销）统一 404 NOT_FOUND；kind 词表外 → 400；
    grant 的 kind 词表外同罚；零副作用。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rv-miss@example.com")
    try:
        uid = await _seed_user(pg, "nominal@example.com")
        missing = await _revoke(client, uid)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        bad_kind = await _revoke(client, uid, kind="super_admin", key="k-bad-kind")
        assert bad_kind.status_code == 400
        assert bad_kind.json()["error"]["code"] == "VALIDATION_ERROR"
        bad_grant = await _grant(client, uid, kind="bogus", key="k-bad-grant")
        assert bad_grant.status_code == 400
        assert bad_grant.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await _count(pg, "user_entitlements") == 0
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()


async def test_grant_idempotent_replay_and_payload_conflict(pg, admin_env):
    """同 key 同载荷原样重放（201 同响应体，零新行零新审计）；DELETE 同 key 异
    reason → 409 IDEMPOTENCY_CONFLICT（reason 参与哈希）；DELETE 重放原响应
    （幂等命中先于状态门，重放不要求行仍存在）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="g-idem@example.com")
    try:
        uid = await _seed_user(pg, "gi@example.com")
        first = await _grant(client, uid, key="k-g-replay")
        assert first.status_code == 201
        replay = await _grant(client, uid, key="k-g-replay")
        assert replay.status_code == 201
        assert replay.json() == first.json()
        assert await _count(pg, "user_entitlements") == 1
        assert await _count(pg, "audit_logs") == 1  # 重放零新副作用

        rev = await _revoke(client, uid, key="k-g-r1")
        assert rev.status_code == 200
        re_revoke = await _revoke(client, uid, key="k-g-r1")
        assert re_revoke.status_code == 200  # 重放原响应（行已删也照放）
        assert re_revoke.json() == rev.json()
        assert await _count(pg, "audit_logs", "action = 'user.entitlement.revoke'") == 1

        conflict = await _revoke(client, uid, reason="另一理由", key="k-g-r1")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert await _count(pg, "audit_logs", "action = 'user.entitlement.revoke'") == 1
    finally:
        await client.aclose()


# ---------- 门与壳负例（四端点全挂三重门）----------


async def test_non_admin_403_forbidden_all_four_endpoints(pg, admin_env):
    """非 admin（无 TOTP + MFA 会话齐全）→ 四端点（含 GET）全 403 FORBIDDEN；
    业务零副作用（门先于幂等/reason/状态门/服务）。"""
    client = await _user_client(pg, admin_env, email="plain@example.com")
    try:
        get = await client.get(_USERS)
        assert get.status_code == 403
        assert get.json()["error"]["code"] == "FORBIDDEN"
        post = await _grant(client, str(_uuid.uuid4()), key="k-na-g")
        assert post.status_code == 403
        assert post.json()["error"]["code"] == "FORBIDDEN"
        put = await _put_quotas(client, str(_uuid.uuid4()), key="k-na-q")
        assert put.status_code == 403
        assert put.json()["error"]["code"] == "FORBIDDEN"
        delete = await _revoke(client, str(_uuid.uuid4()), key="k-na-r")
        assert delete.status_code == 403
        assert delete.json()["error"]["code"] == "FORBIDDEN"
        assert await _count(pg, "user_entitlements") == 0
        assert await _count(pg, "user_quotas") == 0
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()


async def test_admin_mfa_window_expired_403_read_and_write(pg, admin_env):
    """12h 门过期（回拨 13h）→ 读写双向 403 ADMIN_MFA_REQUIRED（门先于幂等）。"""
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="stale@example.com")
    try:
        await _rewind_mfa_verified(pg, admin_id, hours=13)
        get = await client.get(_USERS)
        assert get.status_code == 403
        assert get.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
        post = await _grant(client, str(_uuid.uuid4()), key="k-exp-g")
        assert post.status_code == 403
        assert post.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
    finally:
        await client.aclose()


async def test_reason_and_idem_key_gates_400(pg, admin_env):
    """reason 缺失/空白 → 400 ADMIN_REASON_REQUIRED（PUT/POST/DELETE 三写端点）；
    缺 Idempotency-Key → 400 VALIDATION_ERROR；零副作用。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="reason@example.com")
    try:
        uid = str(_uuid.uuid4())
        no_reason = await client.put(
            f"{_USERS}/{uid}/quotas",
            json={"max_daily_tasks": 5},
            headers={"Idempotency-Key": "k-no-reason-q"},
        )
        assert no_reason.status_code == 400
        assert no_reason.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        blank = await _put_quotas(
            client, uid, body={"max_daily_tasks": 5, "reason": "   "}, key="k-blank-q"
        )
        assert blank.status_code == 400
        assert blank.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        no_reason_g = await client.post(
            f"{_USERS}/{uid}/entitlements",
            json={"kind": "expert_author"},
            headers={"Idempotency-Key": "k-no-reason-g"},
        )
        assert no_reason_g.status_code == 400
        assert no_reason_g.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        no_reason_r = await client.request(
            "DELETE",
            f"{_USERS}/{uid}/entitlements",
            json={"kind": "expert_author"},
            headers={"Idempotency-Key": "k-no-reason-r"},
        )
        assert no_reason_r.status_code == 400
        assert no_reason_r.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        no_key = await client.put(
            f"{_USERS}/{uid}/quotas", json={"max_daily_tasks": 5, "reason": "ok"}
        )
        assert no_key.status_code == 400
        assert no_key.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await _count(pg, "user_quotas") == 0
        assert await _count(pg, "user_entitlements") == 0
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()
