"""admin 审计查询与产物下载端到端测试（Phase 7 T7；Sup §6:146 + D7h/D10-11）。

- HTTP 层经 httpx.AsyncClient(ASGITransport) 驱动真实 app（T2/T4 同款 PG-httpx
  形态；admin_client 直驱全局 app）；
- GET /api/admin/audit-logs：六维过滤（actor_id/action/target_type/target_id/
  since/until）全可选 + created_at DESC 分页；元数据读免 reason；信封
  {data:{items,total,page,size}}，item 九字段钉死；
- GET /api/admin/tasks/{task_id}/artifacts/{file_id}/download：reason 查询参数
  必带（缺/空 400 ADMIN_REASON_REQUIRED，>2000 400 VALIDATION_ERROR）；
  admin 面语义 = 他人任务产物 200 且审计行在（resolve_download 的 owner_id 仅
  解析不过滤——admin_read policy USING(true)）；读审计变体 = 先 INSERT
  AuditLog + flush 成功后才 resolve_download（审计失败注入断言：flush 抛错 →
  500 且 resolve_download 未被触达 → 无 FileResponse）；失败路径（404）不留
  审计残留（端点壳事务回滚）；
- 产物造数复用 test_v2_task_artifacts 同款管道（seed_task_user/seed_provider/
  seed_running_task + owner_tx 内 register_output）；物理存储注入
  rt.storage = TaskStorage(tmp_path)（V2Runtime setattr 注入形态）使 HTTP 侧
  runtime.storage 与造数同根；审计行种子一律 superuser（audit_logs app 零授权、
  admin 全 DML——0001:1023-1026）。
"""

import json
import urllib.parse
import uuid as _uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.main import app
from backend.v2 import admin_audit_service
from backend.v2.runtime import owner_session
from backend.v2.session_service import COOKIE_NAME, create_session
from backend.v2.task_artifacts import register_output
from backend.v2.task_storage import TaskStorage
from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import admin_client
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import owner_tx, seed_input_file, seed_running_task, seed_task_user

# admin_env 夹具发现形态：pytest 经模块命名空间发现夹具，「import + 参数同名」
# 触发 ruff F811——以赋值别名引入（T1 钉死形态，test_v2_admin_reviews.py 同款）。
admin_env = _vah.admin_env

_UA = "AgentCraft-AdminAuditTest/1.0"
_AUDIT = "/api/admin/audit-logs"
_BASE_TS = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def _dl(tid: str, fid: str) -> str:
    return f"/api/admin/tasks/{tid}/artifacts/{fid}/download"


# ---------- 种子与复核助手（superuser 绕 RLS/授权）----------


@pytest.fixture
async def app_engine(role_engine):
    from tests.conftest import APP_ROLE

    engine = role_engine(APP_ROLE)
    yield engine
    await engine.dispose()


async def _seed_audit(
    pg,
    *,
    actor_id: str,
    action: str,
    target_type: str,
    target_id: str | None,
    created_at: datetime,
    reason: str = "理由",
    detail: dict | None = None,
    request_id: str | None = None,
) -> None:
    """superuser 直播 audit_logs 行（actor_id 有 FK users——调用方先种用户）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO audit_logs (id, actor_id, action, target_type, target_id, "
                "reason, request_id, detail, created_at) "
                "VALUES (gen_random_uuid(), CAST(:a AS uuid), :ac, :tt, CAST(:ti AS uuid), "
                ":r, :rq, CAST(:d AS jsonb), :ts)"
            ),
            {
                "a": actor_id,
                "ac": action,
                "tt": target_type,
                "ti": target_id,
                "r": reason,
                "rq": request_id,
                "d": json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                "ts": created_at,
            },
        )


async def _count(pg, where: str = "true", params: dict | None = None) -> int:
    async with pg.engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM audit_logs WHERE {where}"), params or {})
        ).scalar_one()


async def _one(pg, sql: str, params: dict | None = None) -> dict:
    async with pg.engine.connect() as conn:
        return dict((await conn.execute(text(sql), params or {})).mappings().one())


async def _running_round(pg, tid: str) -> tuple[str, int]:
    """任务的 running 轮 (round_id, lease_epoch)（seed_running_task 置 epoch=1）。"""
    async with pg.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, lease_epoch FROM task_rounds "
                    "WHERE task_id = :t AND state = 'running'"
                ),
                {"t": tid},
            )
        ).first()
    assert row is not None, "种子未产生 running 轮"
    return str(row[0]), int(row[1])


async def _seed_artifact(
    pg,
    app_engine,
    rt,
    tmp_path,
    email: str,
    *,
    file_name: str = "交付.bin",
    content: bytes = b"ADMIN DOWNLOAD ME",
) -> tuple[str, str, str, str]:
    """普通用户 running 任务 + register_output 产物全链；返回 (uid, tid, fid, sha)。
    rt.storage 注入 tmp 根——HTTP 侧 runtime.storage 与造数物理同根。"""
    uid = await seed_task_user(pg, email)
    pid = await seed_provider(pg, uid)
    tid = await seed_running_task(pg, uid, pid)
    rid, epoch = await _running_round(pg, tid)
    rt.storage = TaskStorage(tmp_path)
    async with owner_tx(app_engine, uid) as db:
        out = await register_output(
            db,
            rt.storage,
            owner_id=uid,
            task_id=tid,
            file_name=file_name,
            content=content,
            round_id=rid,
            lease_epoch=epoch,
        )
    return uid, tid, str(out["file"]["id"]), str(out["file"]["sha256"])


async def _user_client(pg, rt, *, email: str) -> httpx.AsyncClient:
    """role='user'（无 TOTP）+ mfa_verified 会话客户端——若 admin 门②③先行会误报
    ADMIN_MFA_REQUIRED，403 FORBIDDEN 即门序① role 门先行的证据（T4 同款）。"""
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
        transport=httpx.ASGITransport(app=app, client=("10.9.0.9", 51001)),
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


# ---------- 审计查询（GET /api/admin/audit-logs）----------


async def test_list_audit_logs_filters_pagination_ordering_and_item_shape(pg, admin_env):
    """六维过滤全可选 + created_at DESC 稳定序分页；信封 {items,total,page,size}；
    item 九字段精确等值（id/actor_id/action/target_type/target_id/reason/
    request_id/detail/created_at）；detail JSONB 原样出队。"""
    actor_a = await seed_task_user(pg, "audit-actor-a@x.test")
    actor_b = await seed_task_user(pg, "audit-actor-b@x.test")
    t1, t2, t3 = str(_uuid.uuid4()), str(_uuid.uuid4()), str(_uuid.uuid4())
    # created_at 递增：r1 < r2 < r3 < r4 → DESC 期望 [r4, r3, r2, r1]
    await _seed_audit(
        pg,
        actor_id=actor_a,
        action="user.suspend",
        target_type="user",
        target_id=t1,
        created_at=_BASE_TS,
    )  # r1
    await _seed_audit(
        pg,
        actor_id=actor_a,
        action="user.suspend",
        target_type="user",
        target_id=t2,
        created_at=_BASE_TS + timedelta(hours=1),
        reason="违规操作",
    )  # r2
    await _seed_audit(
        pg,
        actor_id=actor_a,
        action="user.unsuspend",
        target_type="user",
        target_id=t1,
        created_at=_BASE_TS + timedelta(hours=2),
        detail={"k": "v"},
    )  # r3
    await _seed_audit(
        pg,
        actor_id=actor_b,
        action="expert.approve_publish",
        target_type="expert_revision",
        target_id=t3,
        created_at=_BASE_TS + timedelta(hours=3),
        request_id="req-1",
    )  # r4

    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="audit-q@example.com")
    try:
        resp = await client.get(_AUDIT)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert set(data) == {"items", "total", "page", "size"}
        assert data["total"] == 4 and data["page"] == 1 and data["size"] == 20
        assert [it["action"] for it in data["items"]] == [
            "expert.approve_publish",
            "user.unsuspend",
            "user.suspend",
            "user.suspend",
        ]
        item = data["items"][0]  # r4 全字段钉死
        assert set(item) == {
            "id",
            "actor_id",
            "action",
            "target_type",
            "target_id",
            "reason",
            "request_id",
            "detail",
            "created_at",
        }
        assert item["actor_id"] == str(actor_b)
        assert item["target_id"] == t3
        assert item["reason"] == "理由"
        assert item["request_id"] == "req-1"
        assert item["detail"] is None
        assert item["created_at"] == (_BASE_TS + timedelta(hours=3)).isoformat()
        assert data["items"][1]["detail"] == {"k": "v"}  # r3 JSONB 原样

        by_action = (await client.get(_AUDIT, params={"action": "user.suspend"})).json()["data"]
        assert by_action["total"] == 2
        assert [it["action"] for it in by_action["items"]] == ["user.suspend", "user.suspend"]
        assert by_action["items"][0]["reason"] == "违规操作"

        by_actor = (await client.get(_AUDIT, params={"actor_id": str(actor_a)})).json()["data"]
        assert by_actor["total"] == 3 and [it["action"] for it in by_actor["items"]] == [
            "user.unsuspend",
            "user.suspend",
            "user.suspend",
        ]
        by_target_type = (await client.get(_AUDIT, params={"target_type": "user"})).json()["data"]
        assert by_target_type["total"] == 3
        by_target = (await client.get(_AUDIT, params={"target_id": t1})).json()["data"]
        assert by_target["total"] == 2 and [it["action"] for it in by_target["items"]] == [
            "user.unsuspend",
            "user.suspend",
        ]
        combo = (
            await client.get(_AUDIT, params={"action": "user.suspend", "actor_id": str(actor_b)})
        ).json()["data"]
        assert combo["total"] == 0 and combo["items"] == []

        paged = (await client.get(_AUDIT, params={"page": 2, "size": 2})).json()["data"]
        assert paged["total"] == 4 and paged["page"] == 2 and paged["size"] == 2
        assert [it["action"] for it in paged["items"]] == ["user.suspend", "user.suspend"]
        assert paged["items"][0]["reason"] == "违规操作"
    finally:
        await client.aclose()


async def test_list_audit_logs_time_window_inclusive_and_naive_utc(pg, admin_env):
    """since/until 双端闭区间（=边界行命中）；naive ISO 视为 UTC（timestamptz
    比较等价 aware 同刻）；非法 ISO → 400 VALIDATION_ERROR。"""
    actor = await seed_task_user(pg, "audit-window@x.test")
    for h in (0, 1, 2):
        await _seed_audit(
            pg,
            actor_id=actor,
            action="tool_catalog.set_enabled",
            target_type="tool",
            target_id=None,
            created_at=_BASE_TS + timedelta(hours=h),
        )
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="audit-w@example.com")
    try:
        since = (await client.get(_AUDIT, params={"since": "2026-09-10T13:00:00Z"})).json()["data"]
        assert (
            since["total"] == 2
            and since["items"][0]["created_at"] == (_BASE_TS + timedelta(hours=2)).isoformat()
        )
        until = (await client.get(_AUDIT, params={"until": "2026-09-10T13:00:00Z"})).json()["data"]
        assert until["total"] == 2
        both = (
            await client.get(
                _AUDIT,
                params={"since": "2026-09-10T13:00:00Z", "until": "2026-09-10T13:00:00Z"},
            )
        ).json()["data"]
        assert both["total"] == 1  # 双端闭区间：恰含边界行
        naive = (await client.get(_AUDIT, params={"since": "2026-09-10T13:00:00"})).json()["data"]
        assert naive["total"] == 2  # naive 视为 UTC，与 aware 同刻等价
        bad = await client.get(_AUDIT, params={"since": "not-a-date"})
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


async def test_list_audit_logs_invalid_params_400(pg, admin_env):
    """非法分页（page<1 / size<1 / size>100）与非 UUID actor_id/target_id →
    400 VALIDATION_ERROR（服务层统一，错误信封一致）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="audit-bad@example.com")
    try:
        for params in (
            {"page": 0},
            {"size": 0},
            {"size": 101},
            {"actor_id": "not-a-uuid"},
            {"target_id": "nope"},
        ):
            bad = await client.get(_AUDIT, params=params)
            assert bad.status_code == 400, params
            assert bad.json()["error"]["code"] == "VALIDATION_ERROR", params
    finally:
        await client.aclose()


# ---------- admin 产物下载（GET /tasks/{task_id}/artifacts/{file_id}/download）----------


async def test_admin_download_other_user_artifact_200_headers_and_audit_row(
    pg, admin_env, app_engine, tmp_path
):
    """admin 面语义：他人任务产物 200（admin_read 可见全量，非 owner 404）；
    attachment + nosniff 头与内容字节一致；审计行在（action/actor/target/reason/
    request_id/detail={task_id,file_id,sha256} 全钉）。"""
    uid, tid, fid, sha = await _seed_artifact(
        pg, app_engine, admin_env, tmp_path, "t7-owner@x.test"
    )
    content = b"ADMIN DOWNLOAD ME"
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="audit-dl@example.com")
    try:
        assert uid != admin_id  # admin 面语义前提：产物属普通用户
        resp = await client.get(_dl(tid, fid), params={"reason": "审计取证下载"})
        assert resp.status_code == 200
        assert resp.content == content
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.headers["x-content-type-options"] == "nosniff"
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert 'filename="__.bin"' in disposition  # 非 ASCII 落 `_` 的 ASCII 兜底
        assert f"filename*=UTF-8''{urllib.parse.quote('交付.bin', safe='')}" in disposition
        audit = await _one(
            pg,
            "SELECT actor_id, target_type, target_id, reason, request_id, detail "
            "FROM audit_logs WHERE action = 'task.artifact.download'",
        )
        assert str(audit["actor_id"]) == admin_id
        assert audit["target_type"] == "task"
        assert str(audit["target_id"]) == tid
        assert audit["reason"] == "审计取证下载"
        assert audit["request_id"] is None
        assert audit["detail"] == {"task_id": tid, "file_id": fid, "sha256": sha}
        assert await _count(pg, "action = 'task.artifact.download'") == 1
    finally:
        await client.aclose()


async def test_admin_download_missing_or_deleted_task_404_no_audit_residue(
    pg, admin_env, app_engine, tmp_path
):
    """不存在任务 404 TASK_NOT_FOUND；已删除任务 404（其 registered 产物行走
    审计预写 → resolve_download 404 → 端点壳事务回滚，审计零残留）。"""
    uid = await seed_task_user(pg, "t7-deleted-owner@x.test")
    pid = await seed_provider(pg, uid)
    deleted_tid = await seed_task_for_provider(pg, uid, pid, status="deleted")
    gone_fid = await seed_input_file(
        pg,
        uid,
        deleted_tid,
        size_bytes=4,
        state="registered",
        direction="output",
        file_name="gone.bin",
    )
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="audit-404@example.com")
    try:
        missing = await client.get(
            _dl(str(_uuid.uuid4()), str(_uuid.uuid4())), params={"reason": "取证"}
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "TASK_NOT_FOUND"
        deleted = await client.get(_dl(deleted_tid, gone_fid), params={"reason": "取证"})
        assert deleted.status_code == 404
        assert deleted.json()["error"]["code"] == "TASK_NOT_FOUND"
        assert await _count(pg, "action = 'task.artifact.download'") == 0
    finally:
        await client.aclose()


async def test_admin_download_non_output_or_unregistered_404(pg, admin_env):
    """非产物面/未 registered → 404 FILE_NOT_FOUND 且不写审计（预取 SELECT 落空
    短路）：input 行、output-staged 行、存活任务上的随机 fid。"""
    uid = await seed_task_user(pg, "t7-nondl-owner@x.test")
    pid = await seed_provider(pg, uid)
    tid = await seed_task_for_provider(pg, uid, pid)  # queued 存活任务
    input_fid = await seed_input_file(pg, uid, tid, size_bytes=1, file_name="i.txt")
    staged_fid = await seed_input_file(
        pg, uid, tid, size_bytes=2, state="staged", direction="output", file_name="o.txt"
    )
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="audit-fnf@example.com")
    try:
        for fid in (input_fid, staged_fid, str(_uuid.uuid4())):
            resp = await client.get(_dl(tid, fid), params={"reason": "取证"})
            assert resp.status_code == 404, fid
            assert resp.json()["error"]["code"] == "FILE_NOT_FOUND", fid
        assert await _count(pg, "action = 'task.artifact.download'") == 0
    finally:
        await client.aclose()


async def test_admin_download_reason_gates_400(pg, admin_env):
    """reason 查询参数门（壳层 require_admin_reason，先于一切 DB 访问）：缺失/
    空白 → 400 ADMIN_REASON_REQUIRED；>2000 → 400 VALIDATION_ERROR；零审计。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="audit-r@example.com")
    try:
        no_reason = await client.get(_dl(str(_uuid.uuid4()), str(_uuid.uuid4())))
        assert no_reason.status_code == 400
        assert no_reason.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        blank = await client.get(
            _dl(str(_uuid.uuid4()), str(_uuid.uuid4())), params={"reason": "   "}
        )
        assert blank.status_code == 400
        assert blank.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        too_long = await client.get(
            _dl(str(_uuid.uuid4()), str(_uuid.uuid4())), params={"reason": "长" * 2001}
        )
        assert too_long.status_code == 400
        assert too_long.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await _count(pg) == 0
    finally:
        await client.aclose()


async def test_admin_download_audit_failure_refuses_read(
    pg, admin_env, app_engine, tmp_path, monkeypatch
):
    """读审计红线注入断言：flush 抛错 → 审计先行失败 → resolve_download 未被
    触达（sentinel 零调用，钉死「先审计后读」次序）→ 异常向上 = 无 FileResponse
    （拒读）；事务回滚审计零残留。补丁在 admin_client 建会话之后挂（避免误伤
    setup commit）。"""
    uid, tid, fid, _sha = await _seed_artifact(
        pg, app_engine, admin_env, tmp_path, "t7-boom-owner@x.test"
    )
    calls: list[int] = []

    async def _boom_flush(self, *args, **kwargs):
        raise RuntimeError("flush-boom")

    async def _sentinel(*args, **kwargs):
        calls.append(1)
        raise AssertionError("resolve_download must not run before audit flush")

    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="audit-boom@example.com")
    try:
        monkeypatch.setattr(AsyncSession, "flush", _boom_flush)
        monkeypatch.setattr(admin_audit_service, "resolve_download", _sentinel)
        # ServerErrorMiddleware 在发送 500 信封后向 ASGI 客户端回传异常（main.py
        # 通用兜底，HTTP 层 500 断言不可靠——test_v2_deletion_flow 同款先例）：
        # 异常向上 = 无 FileResponse（读取面被拒）。
        with pytest.raises(RuntimeError, match="flush-boom"):
            await client.get(_dl(tid, fid), params={"reason": "审计注入"})
        assert calls == []  # 审计 flush 先行失败 → 读取面零触达
        assert await _count(pg, "action = 'task.artifact.download'") == 0  # 回滚零残留
    finally:
        await client.aclose()


async def test_admin_download_non_uuid_path_400(pg, admin_env):
    """非 UUID 路径参数 → 400 VALIDATION_ERROR（冻结 _parse_id 经 AgentCraftError
    信封透传，review 域同款）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="audit-uuid@example.com")
    try:
        resp = await client.get(_dl(str(_uuid.uuid4()), "not-a-uuid"), params={"reason": "取证"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


# ---------- 门负例（两路由全挂三重门）----------


async def test_audit_routes_role_gate_first_403_no_side_effects(pg, admin_env):
    """非 admin（无 TOTP + mfa_verified 会话齐全——403 只能来自①role 门）→ 两路由
    全 403 FORBIDDEN；业务零副作用。"""
    client = await _user_client(pg, admin_env, email="audit-plain@example.com")
    try:
        get = await client.get(_AUDIT)
        assert get.status_code == 403
        assert get.json()["error"]["code"] == "FORBIDDEN"
        download = await client.get(
            _dl(str(_uuid.uuid4()), str(_uuid.uuid4())), params={"reason": "取证"}
        )
        assert download.status_code == 403
        assert download.json()["error"]["code"] == "FORBIDDEN"
        assert await _count(pg) == 0
    finally:
        await client.aclose()


async def test_audit_routes_mfa_window_expired_403(pg, admin_env):
    """12h 门过期（回拨 13h）→ 两路由全 403 ADMIN_MFA_REQUIRED（读写全受门，
    Sup:126；门先于 reason 门——请求带合法 reason 仍被门拦截）。"""
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="audit-stale@example.com")
    try:
        await _rewind_mfa_verified(pg, admin_id, hours=13)
        get = await client.get(_AUDIT)
        assert get.status_code == 403
        assert get.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
        download = await client.get(
            _dl(str(_uuid.uuid4()), str(_uuid.uuid4())), params={"reason": "取证"}
        )
        assert download.status_code == 403
        assert download.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"
    finally:
        await client.aclose()
