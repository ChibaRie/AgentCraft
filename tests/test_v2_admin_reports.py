"""admin 举报面端到端测试（Phase 7 T5；Sup §6:144-145）。

- HTTP 层经 httpx.AsyncClient(ASGITransport) 驱动真实 app（T3a/T3b 同款 PG-httpx
  形态；admin_client 直驱全局 app）；
- 种子/复核一律 superuser（pg.engine）：reports 属 owner-RLS（reporter 维度）且
  admin 无 INSERT，处置走 reports_admin_read/_update（0001:1127-1131）；
- ban_author 全链（report actioned/作者 suspended+entitlement 硬删/审计
  report.ban_author/级联 receipts 并入响应）与级联失败窗口（状态已翻转不回滚）
  为 brief 钉死断言项；幂等重放返回提交时刻基线（cascade=null，不重跑级联）。
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from backend.main import app
from backend.v2 import admin_user_service
from backend.v2.ids import uuid7
from backend.v2.runtime import owner_session
from backend.v2.session_service import COOKIE_NAME, create_session
from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import admin_client
from tests.v2_content_helpers import seed_entitlement, seed_entity_with_revision
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import seed_task_user

# admin_env 夹具发现形态：pytest 经模块命名空间发现夹具，「import + 参数同名」
# 触发 ruff F811——以赋值别名引入（T1 钉死形态）。
admin_env = _vah.admin_env

_UA = "AgentCraft-AdminReportsTest/1.0"
_REPORTS = "/api/admin/reports"


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


async def _seed_user(pg, email: str) -> str:
    """播种普通用户行（reporter/author 等外键载体——reports.reporter_id、
    experts.owner_id 均真实 FK users.id，伪 UUID 会 IntegrityError），返回 id。"""
    uid = str(_uuid.uuid4())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, mfa_secret_enc) "
                "VALUES (:i, :e, 'h', 'user', 'active', NULL)"
            ),
            {"i": uid, "e": email},
        )
    return uid


async def _seed_report(
    pg,
    reporter_id: str,
    target_type: str,
    target_id: str,
    *,
    status: str = "open",
    created_at: datetime | None = None,
) -> str:
    """superuser 造 report 行（status NOT NULL 且无 server_default，0001:361）。"""
    rid = str(_uuid.uuid4())
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO reports (id, reporter_id, target_type, target_id, status, "
                "reason, created_at) VALUES (:i, CAST(:u AS uuid), :tt, CAST(:t AS uuid), "
                ":s, '测试', :ca)"
            ),
            {
                "i": rid,
                "u": reporter_id,
                "tt": target_type,
                "t": target_id,
                "s": status,
                "ca": created_at or datetime.now(timezone.utc),
            },
        )
    return rid


async def _seed_session(pg, rt, uid: str) -> str:
    """为目标用户种一个活跃会话（owner_session 单事务；revoke_all 生效实证的承载）。"""
    async with owner_session(rt, uid) as db:
        token, _csrf = await create_session(db, user_id=_uuid.UUID(uid), device_label=_UA)
    return token


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
    role 门先行的证据（test_v2_admin_users 同形态）。"""
    uid = await _seed_user(pg, email)
    async with owner_session(rt, uid) as db:
        token, csrf = await create_session(
            db, user_id=_uuid.UUID(uid), device_label=_UA, mfa_verified=True
        )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.8.0.9", 51004)),
        base_url="http://testserver",
        headers={"User-Agent": _UA, "X-CSRF-Token": csrf},
        cookies={COOKIE_NAME: token},
    )


# ---------- 请求助手 ----------


async def _resolve(client, report_id: str, *, body: dict | None = None, key: str = "k-r1"):
    payload = body if body is not None else {"action": "dismiss", "reason": "证据不足"}
    return await client.post(
        f"{_REPORTS}/{report_id}/resolve", json=payload, headers={"Idempotency-Key": key}
    )


# ---------- 队列（GET，元数据读免 reason 免幂等）----------


@pytest.mark.asyncio
async def test_reports_queue_open_only_order_pagination_and_invalid_params(pg, admin_env):
    """队列仅含 open（dismissed 不出队），created_at DESC + id DESC 稳定序，
    {items,total,page,size} 信封；非法分页 400。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rq@example.com")
    try:
        reporter = await _seed_user(pg, "rpt-q@example.com")
        base = datetime.now(timezone.utc)
        older = await _seed_report(
            pg, reporter, "expert_revision", str(uuid7()), created_at=base - timedelta(hours=2)
        )
        newer = await _seed_report(
            pg, reporter, "skill_revision", str(uuid7()), created_at=base - timedelta(hours=1)
        )
        await _seed_report(
            pg,
            reporter,
            "expert_revision",
            str(uuid7()),
            status="dismissed",
            created_at=base,
        )

        resp = await client.get(_REPORTS)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2 and data["page"] == 1 and data["size"] == 20
        assert [item["id"] for item in data["items"]] == [newer, older]
        assert set(data["items"][0]) == {
            "id",
            "target_type",
            "target_id",
            "status",
            "reason",
            "created_at",
        }
        assert data["items"][0]["status"] == "open"

        page2 = await client.get(_REPORTS, params={"page": 2, "size": 1})
        assert [item["id"] for item in page2.json()["data"]["items"]] == [older]
        assert page2.json()["data"]["total"] == 2

        for params in ({"page": 0}, {"size": 0}, {"size": 101}):
            bad = await client.get(_REPORTS, params=params)
            assert bad.status_code == 400, params
            assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


# ---------- dismiss / takedown 薄壳透传 ----------


@pytest.mark.asyncio
async def test_resolve_dismiss_passthrough_then_already_resolved_409(pg, admin_env):
    """dismiss 透传（200 {report_id,status:dismissed}）；已 resolved 二次处置
    （异 key）→ 409 REPORT_ALREADY_RESOLVED。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rd@example.com")
    try:
        reporter = await _seed_user(pg, "rpt-d@example.com")
        rid = await _seed_report(pg, reporter, "expert_revision", str(uuid7()))
        resp = await _resolve(client, rid)
        assert resp.status_code == 200
        assert resp.json()["data"] == {"report_id": rid, "status": "dismissed"}
        assert (
            await _one(pg, "SELECT status FROM reports WHERE id = CAST(:r AS uuid)", {"r": rid})
        )["status"] == "dismissed"

        again = await _resolve(client, rid, key="k-r2")
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "REPORT_ALREADY_RESOLVED"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resolve_takedown_passthrough_row_values(pg, admin_env):
    """takedown 透传（200 actioned + takedown_revision_id）：实体回 draft/指针
    NULL/revision archived 行值复核（D15 服务语义已在服务层钉死，此处钉透传）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rt-tk@example.com")
    try:
        author = await _seed_user(pg, "ath-tk@example.com")
        reporter = await _seed_user(pg, "rpt-tk@example.com")
        entity_id, revision_id = await seed_entity_with_revision(
            pg,
            author,
            "experts",
            entity_status="published",
            revision_status="published",
            with_pointer=True,
        )
        rid = await _seed_report(pg, reporter, "expert_revision", revision_id)
        resp = await _resolve(
            client, rid, body={"action": "takedown_revision", "reason": "确认违规"}, key="k-tk1"
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == {
            "report_id": rid,
            "status": "actioned",
            "takedown_revision_id": revision_id,
        }
        row = await _one(
            pg,
            "SELECT status, published_revision_id FROM experts WHERE id = CAST(:e AS uuid)",
            {"e": entity_id},
        )
        assert row["status"] == "draft" and row["published_revision_id"] is None
        rev_row = await _one(
            pg,
            "SELECT status FROM expert_revisions WHERE id = CAST(:r AS uuid)",
            {"r": revision_id},
        )
        assert rev_row["status"] == "archived"
    finally:
        await client.aclose()


# ---------- ban_author 全链（原子 + 级联两段拆分，D3）----------


@pytest.mark.asyncio
async def test_resolve_ban_author_full_chain_row_values_and_cascade_receipts(pg, admin_env):
    """ban_author 全链：report actioned + 作者 suspended + entitlement 活跃行硬删
    （行值）+ 审计 report.ban_author；提交后级联 receipts 并入响应（会话软撤销
    sessions_revoked=1 + queued 任务两段式回收 flipped_tasks=1/cancelled_rounds=1）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rb@example.com")
    try:
        author = str(await seed_task_user(pg, "ban-victim@example.com"))
        reporter = await _seed_user(pg, "rpt-bf@example.com")
        await seed_entitlement(pg, author)
        pid = await seed_provider(pg, author)
        tid = await seed_task_for_provider(pg, author, pid, status="queued")
        await _seed_session(pg, admin_env, author)
        _, revision_id = await seed_entity_with_revision(
            pg, author, "experts", entity_status="published", revision_status="published"
        )
        rid = await _seed_report(pg, reporter, "expert_revision", revision_id)

        resp = await _resolve(
            client, rid, body={"action": "ban_author", "reason": "屡次违规"}, key="k-ban1"
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == {
            "report_id": rid,
            "status": "actioned",
            "banned_user_id": author,
            "suspension": {
                "user_id": author,
                "before_status": "active",
                "status": "suspended",
                "deadline_cleared": False,
                "entitlement_revoked": True,
            },
            "cascade": {
                "sessions_revoked": 1,
                "tokens_invalidated": 0,
                "flipped_tasks": 1,
                "cancelled_rounds": 1,
                "stopped": 0,
            },
        }
        assert (
            await _one(pg, "SELECT status FROM users WHERE id = CAST(:u AS uuid)", {"u": author})
        )["status"] == "suspended"
        assert (
            await _count(
                pg,
                "user_entitlements",
                "user_id = CAST(:u AS uuid) AND revoked_at IS NULL",
                {"u": author},
            )
            == 0
        )
        assert (
            await _one(pg, "SELECT status FROM reports WHERE id = CAST(:r AS uuid)", {"r": rid})
        )["status"] == "actioned"
        task = await _one(
            pg,
            "SELECT status, abort_reason FROM tasks WHERE id = CAST(:t AS uuid)",
            {"t": tid},
        )
        assert task == {"status": "aborted", "abort_reason": "admin_suspended"}
        audit = await _one(
            pg,
            "SELECT actor_id, target_id, reason, detail FROM audit_logs "
            "WHERE action = 'report.ban_author'",
        )
        assert str(audit["target_id"]) == rid
        assert audit["detail"] == {"report_id": rid, "target_user_id": author}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resolve_ban_author_cascade_failure_window(pg, admin_env, monkeypatch):
    """级联失败=有界窗口（D3 已知代价）：级联抛错 → 200 cascade=null 照常返回，
    已提交状态不回滚（用户 suspended/report actioned/审计在）——重试幂等自愈。"""

    async def _boom(runtime, *, target_user_id, executor):
        raise RuntimeError("级联段故障注入")

    monkeypatch.setattr(admin_user_service, "run_suspension_cascade", _boom)
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rf@example.com")
    try:
        author = await _seed_user(pg, "ath-cf@example.com")
        reporter = await _seed_user(pg, "rpt-cf@example.com")
        _, revision_id = await seed_entity_with_revision(
            pg, author, "experts", entity_status="published", revision_status="published"
        )
        rid = await _seed_report(pg, reporter, "expert_revision", revision_id)
        resp = await _resolve(
            client, rid, body={"action": "ban_author", "reason": "r"}, key="k-ban2"
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["cascade"] is None
        assert resp.json()["data"]["suspension"]["status"] == "suspended"
        assert (
            await _one(pg, "SELECT status FROM users WHERE id = CAST(:u AS uuid)", {"u": author})
        )["status"] == "suspended"
        assert (
            await _one(pg, "SELECT status FROM reports WHERE id = CAST(:r AS uuid)", {"r": rid})
        )["status"] == "actioned"
        assert await _count(pg, "audit_logs", "action = 'report.ban_author'") == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resolve_ban_author_idem_replay_and_payload_conflict(pg, admin_env, monkeypatch):
    """幂等重放返回提交时刻基线（cascade=null，重放不重跑级联——重放前注入级联
    故障亦不触发）；同 key 异 reason → 409 IDEMPOTENCY_CONFLICT。"""

    async def _boom(runtime, *, target_user_id, executor):
        raise RuntimeError("级联段故障注入")

    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="ri@example.com")
    try:
        author = await _seed_user(pg, "ath-id@example.com")
        reporter = await _seed_user(pg, "rpt-id@example.com")
        _, revision_id = await seed_entity_with_revision(
            pg, author, "experts", entity_status="published", revision_status="published"
        )
        rid = await _seed_report(pg, reporter, "expert_revision", revision_id)
        body = {"action": "ban_author", "reason": "违规"}
        first = await _resolve(client, rid, body=body, key="k-idem")
        assert first.status_code == 200
        assert first.json()["data"]["cascade"] is not None

        monkeypatch.setattr(admin_user_service, "run_suspension_cascade", _boom)
        replay = await _resolve(client, rid, body=body, key="k-idem")
        assert replay.status_code == 200
        assert replay.json()["data"]["cascade"] is None
        assert replay.json()["data"]["status"] == "actioned"

        conflict = await _resolve(client, rid, body={**body, "reason": "另案处理"}, key="k-idem")
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    finally:
        await client.aclose()


# ---------- 参数与门负例 ----------


@pytest.mark.asyncio
async def test_resolve_invalid_action_bad_uuid_missing_report(pg, admin_env):
    """action 词表外 400；路径参数非 UUID 400；举报不存在 404（失败路径零副作用）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rx@example.com")
    try:
        bad_action = await _resolve(
            client, str(uuid7()), body={"action": "delete_all", "reason": "r"}, key="k-x1"
        )
        assert bad_action.status_code == 400
        assert bad_action.json()["error"]["code"] == "VALIDATION_ERROR"

        bad_uuid = await _resolve(client, "not-a-uuid", key="k-x2")
        assert bad_uuid.status_code == 400
        assert bad_uuid.json()["error"]["code"] == "VALIDATION_ERROR"

        missing = await _resolve(client, str(uuid7()), key="k-x3")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resolve_message_ban_author_400_passthrough(pg, admin_env):
    """message 举报 + ban_author → 400 VALIDATION_ERROR（message 无作者可封）；
    report 保持 open。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rm@example.com")
    try:
        reporter = await _seed_user(pg, "rpt-mb@example.com")
        rid = await _seed_report(pg, reporter, "message", str(uuid7()))
        resp = await _resolve(client, rid, body={"action": "ban_author", "reason": "r"}, key="k-m1")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        assert (
            await _one(pg, "SELECT status FROM reports WHERE id = CAST(:r AS uuid)", {"r": rid})
        )["status"] == "open"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_resolve_reason_and_idem_key_gates_400(pg, admin_env):
    """reason 门：缺/空白 reason → 400 ADMIN_REASON_REQUIRED（幂等 begin 之前）；
    缺 Idempotency-Key 头 → 400。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rg@example.com")
    try:
        rid = str(uuid7())
        no_reason = await _resolve(client, rid, body={"action": "dismiss"}, key="k-g1")
        assert no_reason.status_code == 400
        assert no_reason.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        blank = await _resolve(client, rid, body={"action": "dismiss", "reason": "  "}, key="k-g2")
        assert blank.status_code == 400
        assert blank.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        no_key = await client.post(
            f"{_REPORTS}/{rid}/resolve", json={"action": "dismiss", "reason": "r"}
        )
        assert no_key.status_code == 400
        assert no_key.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_gate_non_admin_403_forbidden_get_and_post(pg, admin_env):
    """非 admin（role=user 无 TOTP）读写双 403 FORBIDDEN——D2① role 门。"""
    client = await _user_client(pg, admin_env, email="notadmin@example.com")
    try:
        get_resp = await client.get(_REPORTS)
        assert get_resp.status_code == 403
        assert get_resp.json()["error"]["code"] == "FORBIDDEN"
        post_resp = await client.post(
            f"{_REPORTS}/{_uuid.uuid4()}/resolve",
            json={"action": "dismiss", "reason": "r"},
            headers={"Idempotency-Key": "k-n1"},
        )
        assert post_resp.status_code == 403
        assert post_resp.json()["error"]["code"] == "FORBIDDEN"
    finally:
        await client.aclose()
