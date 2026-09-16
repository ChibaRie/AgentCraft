"""D8 admin 任务消息/文件/用户任务读端点测试（Phase 8 T4；Sup §10.5）。

- HTTP 层经 httpx.AsyncClient(ASGITransport) 驱动真实 app（T7 admin 审计测试
  同款 PG-httpx 形态；admin_client 直驱全局 app）；
- 内容读两条（GET /tasks/{id}/messages、GET /tasks/{id}/files）：reason 查询
  参数必带（缺/空白 400 ADMIN_REASON_REQUIRED，>2000 400 VALIDATION_ERROR），
  **先审计后读**（action=task.message.read / task.file_list.read，detail=
  {task_id} 零内容材料）；注入实证沿 T7 产物下载形态：flush 抛错 → 读取面
  零触达（sentinel）→ 事务回滚审计零残留；404（不存在/已删除）预取短路
  **不落审计**（§9.11.4 同形）；
- 元数据读两条（GET /tasks/{id} 快照、GET /users/{id}/tasks 列表）：免 reason
  免审计；任务快照 = owner 面 D14 视图同形 + §10.3 expert/provider 展示字段
  （admin_read 上下文）；用户任务列表 status 词表过滤（词表外 400，§9.11.3
  先例）+ 分页 {items,total,page,size}；
- 门负例：全端点挂三重门（非 admin 403 FORBIDDEN / 12h 过期 403
  ADMIN_MFA_REQUIRED）；元数据读端点自身零审计行为钉（T7 迁入 + D8 新端点）；
- admin_read 实证：任务全链属普通用户（uid != admin_id），admin 会话
  *_admin_read USING(true) 读全量（0001 owner_tables 循环；Step 1 已实证）。
"""

import json
import uuid as _uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.main import app
from backend.v2 import admin_audit_service
from backend.v2.runtime import owner_session
from backend.v2.session_service import COOKIE_NAME, create_session
from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import admin_client
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import seed_input_file, seed_task_user

# admin_env 夹具发现形态：import + 参数同名触发 ruff F811——赋值别名引入
# （T1 钉死形态，test_v2_admin_audit.py 同款）。
admin_env = _vah.admin_env

_M = "/api/admin/tasks/{tid}/messages"
_F = "/api/admin/tasks/{tid}/files"
_T = "/api/admin/tasks/{tid}"
_UT = "/api/admin/users/{uid}/tasks"


# ---------- 种子与复核助手（superuser 绕 RLS/授权；0006 冻结触发器对 GUC
# 未设的 superuser 上下文放行）----------


async def _seed_task_chain(pg, email: str, *, status: str = "queued") -> tuple[str, str, str]:
    """普通用户 + provider + 任务全链；返回 (uid, pid, tid)。queued/ready 附带
    event_sequence=1 的 'seed' 用户消息（seed_task_for_provider 联动）。"""
    uid = await seed_task_user(pg, email)
    pid = await seed_provider(pg, uid)
    tid = str(await seed_task_for_provider(pg, uid, pid, status=status))
    return uid, pid, tid


async def _add_messages(pg, tid: str, uid: str, rows: tuple[tuple[int, str, str], ...]) -> None:
    """superuser 追加 task_messages 行 (event_sequence, author, content)。"""
    async with pg.engine.begin() as conn:
        for seq, author, content in rows:
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), CAST(:t AS uuid), "
                    "CAST(:u AS uuid), :s, :a, :c)"
                ),
                {"t": tid, "u": uid, "s": seq, "a": author, "c": content},
            )


async def _audit_count(pg, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM audit_logs WHERE {where}"), params or {})
        ).scalar_one()


async def _audit_one(pg, action: str) -> dict:
    """取该 action 最新一行（created_at DESC/id DESC 稳定序；多读多行时取末次）。"""
    async with pg.engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    text(
                        "SELECT actor_id, target_type, target_id, reason, request_id, detail "
                        "FROM audit_logs WHERE action = :a "
                        "ORDER BY created_at DESC, id DESC LIMIT 1"
                    ),
                    {"a": action},
                )
            )
            .mappings()
            .one()
        )


async def _user_client(pg, rt, *, email: str) -> httpx.AsyncClient:
    """role='user'（无 TOTP）+ mfa_verified 会话客户端——403 只能来自①role 门
    （门序证据形态；test_v2_admin_audit 同款自包含复制）。"""
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
            db,
            user_id=_uuid.UUID(uid),
            device_label="AgentCraft-AdminReadTest/1.0",
            mfa_verified=True,
        )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.11.0.9", 51001)),
        base_url="http://testserver",
        headers={
            "User-Agent": "AgentCraft-AdminReadTest/1.0",
            "X-CSRF-Token": csrf,
        },
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


# ---------- ①② 消息内容读（先审计后读）----------


async def test_admin_messages_reason_200_ascending_full_content_and_audit(pg, admin_env):
    """admin 带 reason → 200 event_sequence 升序全量正文；每次成功读各落一行
    task.message.read（actor/target/reason/detail={task_id} 零内容材料全钉）；
    after 游标 + limit 截断（默认 50 上限 200 由 Query 门）。"""
    uid, _pid, tid = await _seed_task_chain(pg, "t4-msg-owner@x.test")
    await _add_messages(pg, tid, uid, ((2, "assistant", "第二轮回答"), (3, "tool", "工具输出")))
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="t4-msg-admin@example.com")
    try:
        assert uid != admin_id  # admin_read 实证前提：任务属普通用户
        resp = await client.get(_M.format(tid=tid), params={"reason": "举报核查"})
        assert resp.status_code == 200
        items = resp.json()["data"]
        assert [m["event_sequence"] for m in items] == [1, 2, 3]  # 升序
        assert [m["author"] for m in items] == ["user", "assistant", "tool"]
        assert items[0]["content"] == "seed" and items[1]["content"] == "第二轮回答"
        assert set(items[0]) == {"id", "event_sequence", "author", "content", "created_at"}
        assert await _audit_count(pg, "action = 'task.message.read'") == 1
        row = await _audit_one(pg, "task.message.read")
        assert str(row["actor_id"]) == admin_id
        assert row["target_type"] == "task"
        assert str(row["target_id"]) == tid
        assert row["reason"] == "举报核查"
        assert row["request_id"] is None
        assert row["detail"] == {"task_id": tid}  # 零内容材料

        after = (
            await client.get(_M.format(tid=tid), params={"after": 1, "reason": "续拉"})
        ).json()["data"]
        assert [m["event_sequence"] for m in after] == [2, 3]
        limited = (
            await client.get(_M.format(tid=tid), params={"limit": 2, "reason": "采样"})
        ).json()["data"]
        assert [m["event_sequence"] for m in limited] == [1, 2]
        # 每次内容读各落一行审计（三读三行）
        assert await _audit_count(pg, "action = 'task.message.read'") == 3
    finally:
        await client.aclose()


async def test_admin_messages_audit_failure_refuses_read(pg, admin_env, monkeypatch):
    """先审计后读注入实证（T7 产物下载形态）：flush 抛错 → 读取面零触达
    （sentinel 零调用）→ 异常向上无响应体；事务回滚审计零残留。补丁在
    admin_client 建会话之后挂（避免误伤 setup commit）。"""
    _uid, _pid, tid = await _seed_task_chain(pg, "t4-msg-boom-owner@x.test")
    calls: list[int] = []

    async def _boom_flush(self, *args, **kwargs):
        raise RuntimeError("flush-boom")

    async def _sentinel(*args, **kwargs):
        calls.append(1)
        raise AssertionError("list_task_messages must not run before audit flush")

    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="t4-msg-boom@example.com")
    try:
        monkeypatch.setattr(AsyncSession, "flush", _boom_flush)
        monkeypatch.setattr(admin_audit_service, "list_task_messages", _sentinel)
        with pytest.raises(RuntimeError, match="flush-boom"):
            await client.get(_M.format(tid=tid), params={"reason": "注入"})
        assert calls == []  # 审计 flush 先行失败 → 读取面零触达
        assert await _audit_count(pg, "action = 'task.message.read'") == 0
    finally:
        await client.aclose()


async def test_admin_content_reads_reason_gates_400(pg, admin_env):
    """reason 查询参数门（messages/files 两端点，壳层 require_admin_reason，先于
    一切 DB 访问）：缺失/空白 → 400 ADMIN_REASON_REQUIRED；>2000 → 400
    VALIDATION_ERROR；零审计。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="t4-reason@example.com")
    try:
        for url, base in ((_M, {"tid": str(_uuid.uuid4())}), (_F, {"tid": str(_uuid.uuid4())})):
            missing = await client.get(url.format(**base))
            assert missing.status_code == 400, url
            assert missing.json()["error"]["code"] == "ADMIN_REASON_REQUIRED", url
            blank = await client.get(url.format(**base), params={"reason": "   "})
            assert blank.status_code == 400, url
            assert blank.json()["error"]["code"] == "ADMIN_REASON_REQUIRED", url
            too_long = await client.get(url.format(**base), params={"reason": "长" * 2001})
            assert too_long.status_code == 400, url
            assert too_long.json()["error"]["code"] == "VALIDATION_ERROR", url
        assert await _audit_count(pg) == 0
    finally:
        await client.aclose()


# ---------- ③ 文件元数据读（先审计后读，无内容字节）----------


async def test_admin_files_direction_metadata_no_content_and_audit(pg, admin_env):
    """?direction= 词表过滤（input|output，词表外 400 先于审计）：元数据五键
    id/file_name/sha256/size_bytes/state（无 content 字段）；deleted 墓碑不可见；
    每次成功读各落一行 task.file_list.read（detail={task_id} 零内容材料）。"""
    uid, _pid, tid = await _seed_task_chain(pg, "t4-files-owner@x.test")
    await seed_input_file(pg, uid, tid, size_bytes=7, file_name="输入.txt")
    await seed_input_file(
        pg, uid, tid, size_bytes=9, state="staged", direction="output", file_name="out.bin"
    )
    await seed_input_file(pg, uid, tid, size_bytes=3, state="deleted", file_name="墓碑.txt")
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="t4-files-admin@example.com")
    try:
        bad = await client.get(_F.format(tid=tid), params={"direction": "both", "reason": "核查"})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await _audit_count(pg, "action = 'task.file_list.read'") == 0  # 400 先于审计

        resp = await client.get(_F.format(tid=tid), params={"direction": "input", "reason": "核查"})
        assert resp.status_code == 200
        items = resp.json()["data"]
        assert len(items) == 1
        # Phase 9 T2 §10.11(b)：五键拓 id（内联下载消费）；仍无 content
        assert set(items[0]) == {"id", "file_name", "sha256", "size_bytes", "state"}
        assert items[0]["file_name"] == "输入.txt"
        assert items[0]["size_bytes"] == 7 and items[0]["state"] == "staged"
        out = await client.get(_F.format(tid=tid), params={"direction": "output", "reason": "核查"})
        assert [f["file_name"] for f in out.json()["data"]] == ["out.bin"]  # 墓碑不可见

        assert await _audit_count(pg, "action = 'task.file_list.read'") == 2  # 两读两行
        row = await _audit_one(pg, "task.file_list.read")
        assert str(row["actor_id"]) == admin_id
        assert row["target_type"] == "task"
        assert str(row["target_id"]) == tid
        assert row["detail"] == {"task_id": tid}  # 零内容材料
    finally:
        await client.aclose()


async def test_admin_files_audit_failure_refuses_read(pg, admin_env, monkeypatch):
    """files 先审计后读注入实证（与 messages 同构）：flush 抛错 → 读取面
    零触达 → 回滚审计零残留。"""
    _uid, _pid, tid = await _seed_task_chain(pg, "t4-files-boom-owner@x.test")
    calls: list[int] = []

    async def _boom_flush(self, *args, **kwargs):
        raise RuntimeError("flush-boom")

    async def _sentinel(*args, **kwargs):
        calls.append(1)
        raise AssertionError("file metadata read must not run before audit flush")

    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="t4-files-boom@example.com")
    try:
        monkeypatch.setattr(AsyncSession, "flush", _boom_flush)
        monkeypatch.setattr(admin_audit_service, "_task_file_meta_rows", _sentinel)
        with pytest.raises(RuntimeError, match="flush-boom"):
            await client.get(_F.format(tid=tid), params={"direction": "input", "reason": "注入"})
        assert calls == []
        assert await _audit_count(pg, "action = 'task.file_list.read'") == 0
    finally:
        await client.aclose()


# ---------- ④ 用户任务列表（元数据读）----------


async def test_admin_user_tasks_filter_pagination_no_audit(pg, admin_env):
    """GET /users/{uid}/tasks：status 词表过滤（词表外 400 §9.11.3 先例）+
    分页 {items,total,page,size}；items 四键 id/status/abort_reason/created_at；
    deleted 不可见；免 reason、零审计；未知用户 404 NOT_FOUND、非 UUID 400。"""
    uid, pid, _tid = await _seed_task_chain(pg, "t4-ut-owner@x.test")  # queued
    for status in ("completed", "aborted", "deleted"):
        await seed_task_for_provider(pg, uid, pid, status=status)
    async with pg.engine.begin() as conn:  # created_at 拉开使 DESC 序可断言
        await conn.execute(
            text(
                "UPDATE tasks SET created_at = now() - make_interval(secs => s) FROM "
                "(SELECT id, row_number() OVER (ORDER BY created_at) AS n FROM tasks "
                "WHERE owner_id = CAST(:u AS uuid)) AS t(tasks_id, s) "
                "WHERE tasks.id = t.tasks_id AND s IN (1, 2)"
            ),
            {"u": uid},
        )
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="t4-ut-admin@example.com")
    try:
        resp = await client.get(_UT.format(uid=uid))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {"items", "total", "page", "size"}
        assert data["total"] == 3 and data["page"] == 1 and data["size"] == 20  # deleted 不可见
        assert all(
            set(it) == {"id", "status", "abort_reason", "created_at"} for it in data["items"]
        )
        assert [it["status"] for it in data["items"]] == ["aborted", "queued", "completed"]

        by_status = (await client.get(_UT.format(uid=uid), params={"status": "completed"})).json()[
            "data"
        ]
        assert by_status["total"] == 1 and by_status["items"][0]["status"] == "completed"
        bad = await client.get(_UT.format(uid=uid), params={"status": "bogus"})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"

        paged_resp = await client.get(_UT.format(uid=uid), params={"size": 2, "page": 2})
        paged = paged_resp.json()["data"]
        assert paged["total"] == 3 and paged["page"] == 2 and paged["size"] == 2
        assert len(paged["items"]) == 1 and paged["items"][0]["status"] == "completed"

        gone = await client.get(_UT.format(uid=str(_uuid.uuid4())))
        assert gone.status_code == 404
        assert gone.json()["error"]["code"] == "NOT_FOUND"
        bad_uid = await client.get(_UT.format(uid="not-a-uuid"))
        assert bad_uid.status_code == 400
        assert bad_uid.json()["error"]["code"] == "VALIDATION_ERROR"

        assert await _audit_count(pg) == 0  # 元数据读零审计
    finally:
        await client.aclose()


# ---------- ⑤ 任务快照（元数据读，owner 面同形 + §10.3 展示字段）----------


async def test_admin_task_snapshot_owner_shape_with_expert_provider(pg, admin_env):
    """GET /tasks/{tid}：D14 owner 面视图同形 + expert/provider 展示字段
    （expert join 走 admin_read——published 指针语义与 owner 面一致）；免 reason、
    零审计。"""
    _uid, _pid, tid = await _seed_task_chain(pg, "t4-snap-owner@x.test")
    async with pg.engine.begin() as conn:  # superuser 改 content_json（0006 对 GUC 未设放行）
        await conn.execute(
            text(
                "UPDATE expert_revisions SET content_json = CAST(:j AS jsonb) WHERE id = "
                "(SELECT expert_revision_id FROM tasks WHERE id = CAST(:t AS uuid))"
            ),
            {
                "j": json.dumps({"name": "网格专家", "avatar_url": "https://cdn.example/a.png"}),
                "t": tid,
            },
        )
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="t4-snap-admin@example.com")
    try:
        resp = await client.get(_T.format(tid=tid))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {
            "id",
            "status",
            "abort_reason",
            "created_at",
            "input_committed",
            "input_manifest_sha256",
            "event_sequence",
            "active_round",
            "initial_round",
            "counts",
            "expert",
            "provider",
        }
        assert data["id"] == tid and data["status"] == "queued"
        assert data["active_round"] == {**data["active_round"], "state": "pending"}
        assert data["initial_round"] is None
        assert data["counts"] == {"inputs": 0, "outputs": 0}
        assert data["expert"] == {"name": "网格专家", "avatar_url": "https://cdn.example/a.png"}
        assert data["provider"] == {"display_name": "OpenAI", "model": "m"}
        assert await _audit_count(pg) == 0
    finally:
        await client.aclose()


# ---------- ⑥⑦ admin_read 实证与 404 不落审计 ----------


async def test_admin_reads_other_users_task_200_admin_read(pg, admin_env):
    """⑥ 他人任务 200（admin_read USING(true) 实证）：三条任务面端点全 200，
    无任何 owner 过滤（任务属普通用户、请求者为 admin）。"""
    uid, _pid, tid = await _seed_task_chain(pg, "t6-other-owner@x.test")
    await seed_input_file(pg, uid, tid, size_bytes=5, file_name="a.txt")
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="t6-admin@example.com")
    try:
        assert uid != admin_id
        msg = await client.get(_M.format(tid=tid), params={"reason": "取证"})
        assert msg.status_code == 200
        files = await client.get(
            _F.format(tid=tid), params={"direction": "input", "reason": "取证"}
        )
        assert files.status_code == 200
        snap = await client.get(_T.format(tid=tid))
        assert snap.status_code == 200
    finally:
        await client.aclose()


async def test_admin_reads_missing_or_deleted_task_404_no_audit(pg, admin_env):
    """⑦ 不存在/已删除任务 → 404 TASK_NOT_FOUND 且不落审计（预取落空短路，
    §9.11.4 同形）；元数据读同 404。"""
    _uid, pid, _tid = await _seed_task_chain(pg, "t7b-deleted-owner@x.test")
    deleted_tid = str(await seed_task_for_provider(pg, _uid, pid, status="deleted"))
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="t7b-admin@example.com")
    try:
        for tid in (str(_uuid.uuid4()), deleted_tid):
            msg = await client.get(_M.format(tid=tid), params={"reason": "取证"})
            assert msg.status_code == 404, tid
            assert msg.json()["error"]["code"] == "TASK_NOT_FOUND", tid
            files = await client.get(
                _F.format(tid=tid), params={"direction": "input", "reason": "取证"}
            )
            assert files.status_code == 404, tid
            snap = await client.get(_T.format(tid=tid))
            assert snap.status_code == 404, tid
            assert snap.json()["error"]["code"] == "TASK_NOT_FOUND", tid
        assert await _audit_count(pg) == 0  # 404 不落审计
    finally:
        await client.aclose()


# ---------- ⑧ 三重门负例 ----------


async def test_admin_reads_triple_gate_negatives(pg, admin_env):
    """非 admin（无 TOTP + mfa_verified 会话齐全——403 只能来自①role 门）→
    四端点全 403 FORBIDDEN；12h 过期（回拨 13h）→ 四端点全 403
    ADMIN_MFA_REQUIRED（读写全受门，门先于 reason）。"""
    client = await _user_client(pg, admin_env, email="t4-gate-plain@x.test")
    try:
        plain_hits = [
            await client.get(_M.format(tid=str(_uuid.uuid4())), params={"reason": "取证"}),
            await client.get(
                _F.format(tid=str(_uuid.uuid4())), params={"direction": "input", "reason": "取证"}
            ),
            await client.get(_T.format(tid=str(_uuid.uuid4()))),
            await client.get(_UT.format(uid=str(_uuid.uuid4()))),
        ]
        for hit in plain_hits:
            assert hit.status_code == 403
            assert hit.json()["error"]["code"] == "FORBIDDEN"
    finally:
        await client.aclose()

    client2, _csrf, admin_id = await admin_client(pg, admin_env, email="t4-gate-stale@example.com")
    try:
        await _rewind_mfa_verified(pg, admin_id, hours=13)
        stale_hits = [
            await client2.get(_M.format(tid=str(_uuid.uuid4())), params={"reason": "取证"}),
            await client2.get(
                _F.format(tid=str(_uuid.uuid4())), params={"direction": "input", "reason": "取证"}
            ),
            await client2.get(_T.format(tid=str(_uuid.uuid4()))),
            await client2.get(_UT.format(uid=str(_uuid.uuid4()))),
        ]
        for hit in stale_hits:
            assert hit.status_code == 403
            assert hit.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
    finally:
        await client2.aclose()


# ---------- ⑨ 零读审计行为钉 ----------


async def test_admin_metadata_reads_self_write_no_audit(pg, admin_env):
    """⑨ 元数据读端点自身不落审计（T7 迁入钉 + D8 新端点同钉）：audit-logs、
    users 列表、任务快照、用户任务列表四读后 audit_logs 零行。"""
    uid, _pid, tid = await _seed_task_chain(pg, "t9-owner@x.test")
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="t9-admin@example.com")
    try:
        assert (await client.get("/api/admin/audit-logs")).status_code == 200  # T7 元数据读
        assert (await client.get("/api/admin/users")).status_code == 200  # T3a 元数据读
        assert (await client.get(_T.format(tid=tid))).status_code == 200  # D8 元数据读
        assert (await client.get(_UT.format(uid=uid))).status_code == 200  # D8 元数据读
        assert await _audit_count(pg) == 0  # 元数据读零审计行为钉
    finally:
        await client.aclose()
