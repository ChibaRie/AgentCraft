"""Phase 9 T2 契约增量三项测试（Sup §10.11）。

(a) GET /api/skills/public——他人 published skill 公开枚举（非敏感卡五键）；
(b) GET /api/admin/tasks/{id}/files 条目补 id 键（消费内联下载）；
(c) GET /api/admin/reports/{report_id} 详情——message 目标解析 task_id。

形态沿既有先例：PG-httpx 直驱全局 app（test_v2_admin_reads / test_v2_discover_api
同款）；种子走 superuser（owner-RLS 表 admin 无 INSERT policy）。
"""

import uuid as _uuid

from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import admin_client
from tests.v2_content_helpers import SKILL_CONTENT, seed_entity_with_revision
from tests.v2_provider_helpers import auth_client, login
from tests.v2_task_helpers import seed_input_file, seed_task_user

admin_env = _vah.admin_env

_PUBLIC = "/api/skills/public"


async def _seed_active_user(pg, email: str) -> str:
    """普通 active 用户（可登录）——沿 v2_provider_helpers.seed_active_user 形态。"""
    from sqlalchemy import text

    from backend.v2.security import hash_password

    uid = _uuid.uuid4()
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status) "
                "VALUES (:i, :e, :p, 'user', 'active')"
            ),
            {"i": str(uid), "e": email.lower(), "p": hash_password("pw-123456")},
        )
    return str(uid)


# ---------- (a) 公开 skill 枚举 ----------


async def test_public_skills_lists_others_published_only(pg, admin_env):
    """他人已发布 skill 可见；本人 draft 与未发布实体不可见；卡五键无方法论正文。"""
    owner = await _seed_active_user(pg, "t2a-owner@example.com")
    await _seed_active_user(pg, "t2a-viewer@example.com")
    pub_id, pub_rev = await seed_entity_with_revision(
        pg, owner, "skills", content_json=SKILL_CONTENT, with_pointer=True
    )
    await seed_entity_with_revision(pg, owner, "skills", content_json=SKILL_CONTENT)

    client = auth_client()
    try:
        await login(client, "t2a-viewer@example.com", "pw-123456")
        resp = await client.get(_PUBLIC, params={"status": "published"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        items = data["items"]
        assert [i["id"] for i in items] == [pub_id]  # 仅他人 published
        assert items[0]["published_revision_id"] == pub_rev
        assert set(items[0]) == {
            "id",
            "published_revision_id",
            "name",
            "description",
            "category",
        }
        assert "methodology" not in items[0] and "persona" not in items[0]
        assert data["total"] == 1 and data["page"] == 1 and data["page_size"] == 20
    finally:
        await client.aclose()


async def test_public_skills_status_validation_and_auth(pg, admin_env):
    """status 词表外 400 VALIDATION_ERROR；未登录 401（需 get_v2_auth）。"""
    client = auth_client()
    try:
        anon = await client.get(_PUBLIC)
        assert anon.status_code == 401
    finally:
        await client.aclose()

    await _seed_active_user(pg, "t2a-val@example.com")
    client = auth_client()
    try:
        await login(client, "t2a-val@example.com", "pw-123456")
        bad = await client.get(_PUBLIC, params={"status": "draft"})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
        ok = await client.get(_PUBLIC)  # 缺省即 published
        assert ok.status_code == 200
    finally:
        await client.aclose()


# ---------- (b) admin 文件列表 id 键 ----------


async def _task_chain(pg, email: str) -> tuple[str, str]:
    from tests.v2_provider_helpers import seed_provider, seed_task_for_provider

    uid = await seed_task_user(pg, email)
    pid = await seed_provider(pg, uid)
    tid = str(await seed_task_for_provider(pg, uid, pid, status="queued"))
    return uid, tid


async def test_admin_files_items_expose_id_key(pg, admin_env):
    """五键（id/file_name/sha256/size_bytes/state）；id 为 task_files.id，可直接
    喂内联下载（消费面 Sup §10.11(b)）。"""
    uid, tid = await _task_chain(pg, "t2b-owner@example.com")
    await seed_input_file(pg, uid, tid, size_bytes=11, file_name="证据.txt")
    client, _csrf, _aid = await admin_client(pg, admin_env, email="t2b-admin@example.com")
    try:
        resp = await client.get(
            f"/api/admin/tasks/{tid}/files",
            params={"direction": "input", "reason": "核查"},
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert len(items) == 1
        assert set(items[0]) == {"id", "file_name", "sha256", "size_bytes", "state"}
        _uuid.UUID(items[0]["id"])  # 合法 UUID
    finally:
        await client.aclose()


# ---------- (c) 举报详情 task_id 解析 ----------


async def _seed_report(pg, *, target_type: str, target_id: str, reporter_id: str) -> str:
    from sqlalchemy import text

    async with pg.engine.begin() as conn:
        rid = (
            await conn.execute(
                text(
                    "INSERT INTO reports (id, reporter_id, target_type, target_id, status, reason) "
                    "VALUES (gen_random_uuid(), :r, :tt, :tid, 'open', 't2c reason text') "
                    "RETURNING id"
                ),
                {"r": reporter_id, "tt": target_type, "tid": target_id},
            )
        ).scalar_one()
    return str(rid)


async def _seed_message(pg, uid: str, tid: str) -> str:
    from sqlalchemy import text

    async with pg.engine.begin() as conn:
        mid = (
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), :t, :u, 99, 'assistant', "
                    "'reply body') RETURNING id"
                ),
                {"t": tid, "u": uid},
            )
        ).scalar_one()
    return str(mid)


async def test_admin_report_detail_resolves_message_task_id(pg, admin_env):
    """message 目标 → task_id 为所属任务；非 message 目标 → task_id=null；
    响应含 report_brief 六键。"""
    uid, tid = await _task_chain(pg, "t2c-owner@example.com")
    mid = await _seed_message(pg, uid, tid)
    msg_report = await _seed_report(pg, target_type="message", target_id=mid, reporter_id=uid)
    _, expert_rev = await seed_entity_with_revision(
        pg,
        uid,
        "experts",
        entity_status="published",
        revision_status="published",
        with_pointer=False,
    )
    other_report = await _seed_report(
        pg, target_type="expert_revision", target_id=expert_rev, reporter_id=uid
    )

    client, _csrf, _aid = await admin_client(pg, admin_env, email="t2c-admin@example.com")
    try:
        resp = await client.get(f"/api/admin/reports/{msg_report}")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["task_id"] == tid
        assert set(data) == {
            "id",
            "target_type",
            "target_id",
            "status",
            "reason",
            "created_at",
            "task_id",
        }
        assert data["target_type"] == "message" and data["target_id"] == mid

        other = await client.get(f"/api/admin/reports/{other_report}")
        assert other.status_code == 200
        assert other.json()["data"]["task_id"] is None
    finally:
        await client.aclose()


async def test_admin_report_detail_404_and_400(pg, admin_env):
    """非法 UUID → 400 VALIDATION_ERROR；不存在 → 404 NOT_FOUND（含消息已删的
    孤儿 message 举报 → task_id=null）。"""
    client, _csrf, _aid = await admin_client(pg, admin_env, email="t2c-err-admin@example.com")
    try:
        bad = await client.get("/api/admin/reports/not-a-uuid")
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
        missing = await client.get(f"/api/admin/reports/{_uuid.uuid4()}")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"

        uid, _tid = await _task_chain(pg, "t2c-orphan@example.com")
        orphan = await _seed_report(
            pg, target_type="message", target_id=str(_uuid.uuid4()), reporter_id=uid
        )
        resp = await client.get(f"/api/admin/reports/{orphan}")
        assert resp.status_code == 200
        assert resp.json()["data"]["task_id"] is None
    finally:
        await client.aclose()


async def test_admin_report_detail_requires_admin(pg, admin_env):
    """非 admin → 403 FORBIDDEN（三重门）。"""
    await _seed_active_user(pg, "t2c-nonadmin@example.com")
    client = auth_client()
    try:
        await login(client, "t2c-nonadmin@example.com", "pw-123456")
        resp = await client.get(f"/api/admin/reports/{_uuid.uuid4()}")
        assert resp.status_code == 403
    finally:
        await client.aclose()
