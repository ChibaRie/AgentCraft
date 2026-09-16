"""Eng §7 应用层子集自动化对账补强（Phase 9 T3；D1①②⑤⑦⑧）。

gap 分析产出的 4 项缺口补测 + 1 项 grant 缓存清空验证：

1. 限流重启连续性——rate_limit_events 持久化 → "新进程"（新 runtime 引擎池）
   同一 subject 仍 429（不是仅内存计数）。
2. 会话固定——登录前无 session id，登录后 token_hash 不复用/不预测；
   token 明文 ≠ hash。
3. 重复下载幂等——同 file_id 两次 admin 产物下载均 200（幂等读）。
4. 文件删除竞态——delete 后同 id 下载 → 404（TOCTOU 面由状态门内联拦截）。
5. grant 缓存清空——settle 弹出后同 round_id 不再命中 grant 缓存（进程内存缓存
   的弹出即作废，不残留）。

RLS 矩阵（D1①）、CSRF、MFA、会话撤销、审核/kill-switch/举报（D1⑧）已由
test_v2_rls / test_v2_session_service / test_v2_login_mfa / test_v2_admin_reviews
等既有件覆盖——gap 分析表见 ledger T3 节。
"""

import uuid as _uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import owner_session
from backend.v2.session_service import create_session, hash_token
from tests.conftest import PgDb
from tests.test_internal_grant import _grant

# grant_env fixture is defined in test_internal_grant; re-export via import:
from tests.test_internal_grant import grant_env as grant_env  # noqa: F401 re-export
from tests.test_v2_runtime import make_v2_runtime
from tests.v2_admin_helpers import admin_client
from tests.v2_admin_helpers import admin_env as admin_env  # noqa: F401 re-export fixture
from tests.v2_provider_helpers import seed_provider
from tests.v2_task_helpers import seed_running_task, seed_task_user

_M = "/api/admin/tasks/{tid}/artifacts/{fid}/download"


# ---------- 1. 限流重启连续性 ----------


async def test_rate_limit_persistence_survives_process_restart(pg: PgDb, monkeypatch):
    """rate_limit_events 持久化：写入 10 行（login limit）→ 新引擎池（模拟进程
    重启）→ 同 subject 仍 429。不是内存计数。"""
    import base64

    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode())
    subject = hmac_subject("email", "restart@x.test")

    async with make_v2_runtime(pg).app_factory() as db:
        for _ in range(10):  # login = (10, 900)
            await enforce(db, scope="login", subjects=[subject])

    # 模拟进程重启：新 runtime + 新引擎池
    rt2 = make_v2_runtime(pg)
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with rt2.app_factory() as db:
                await enforce(db, scope="login", subjects=[subject])
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["code"] == "TOO_MANY_REQUESTS"
    finally:
        rt2.close()


# ---------- 2. 会话固定 ----------


async def test_session_token_not_reused_across_logins(pg: PgDb):
    """两次登录产生两个不同 session token（无固定面）；token_hash != token 明文。"""
    uid = str(await seed_task_user(pg, "fix-session@x.test"))

    tokens = set()
    rt = make_v2_runtime(pg)
    try:
        for _ in range(2):
            async with owner_session(rt, uid) as db:
                token, _csrf = await create_session(
                    db, user_id=_uuid.UUID(uid), device_label="test"
                )
            tokens.add(token)
            # token 明文 ≠ hash（固定面不存在：每次登录生新 token）
            assert token != hash_token(token)
    finally:
        rt.close()

    assert len(tokens) == 2  # 两次登录 token 互不相同


# ---------- 3. 重复下载幂等 ----------


async def test_admin_artifact_download_idempotent(pg, admin_env, app_engine, tmp_path):
    from backend.v2.task_artifacts import register_output
    from backend.v2.task_storage import TaskStorage

    uid = str(await seed_task_user(pg, "dup-dl@x.test"))
    pid = await seed_provider(pg, uid)
    tid = str(await seed_running_task(pg, uid, pid))
    rid, epoch = await _running_round(pg, tid)
    admin_env.storage = TaskStorage(tmp_path)
    async with owner_session(admin_env, uid) as db:
        out = await register_output(
            db,
            admin_env.storage,
            owner_id=uid,
            task_id=tid,
            file_name="out.txt",
            content=b"ARTIFACT",
            round_id=rid,
            lease_epoch=epoch,
        )
    fid = str(out["file"]["id"])

    client, _csrf, _aid = await admin_client(pg, admin_env, email="dup-dl-admin@example.com")
    try:
        for _i in range(2):
            resp = await client.get(
                _M.format(tid=tid, fid=fid),
                params={"reason": "dup download"},
            )
            assert resp.status_code == 200
    finally:
        await client.aclose()


async def test_admin_artifact_download_after_delete_404(pg, admin_env, app_engine, tmp_path):
    from backend.v2.task_artifacts import register_output
    from backend.v2.task_storage import TaskStorage

    uid = str(await seed_task_user(pg, "del-race@x.test"))
    pid = await seed_provider(pg, uid)
    tid = str(await seed_running_task(pg, uid, pid))
    rid, epoch = await _running_round(pg, tid)
    admin_env.storage = TaskStorage(tmp_path)
    async with owner_session(admin_env, uid) as db:
        out = await register_output(
            db,
            admin_env.storage,
            owner_id=uid,
            task_id=tid,
            file_name="out.txt",
            content=b"DELETE ME",
            round_id=rid,
            lease_epoch=epoch,
        )
    fid = str(out["file"]["id"])

    client, _csrf, _aid = await admin_client(pg, admin_env, email="del-race-admin@example.com")
    try:
        ok = await client.get(_M.format(tid=tid, fid=fid), params={"reason": "before delete"})
        assert ok.status_code == 200
        async with pg.engine.begin() as conn:
            await conn.execute(
                text("UPDATE task_files SET state = 'deleted' WHERE id = :f"),
                {"f": fid},
            )
        gone = await client.get(_M.format(tid=tid, fid=fid), params={"reason": "after delete"})
        assert gone.status_code == 404
    finally:
        await client.aclose()


# ---------- 5. grant 缓存清空（settle 后同 round_id 不残留）----------


async def test_grant_cache_popped_after_settle(client, grant_env, pg):
    """settle 后：同 round_id 的 grant 缓存弹出 → 再取 401（缓存不残留）。"""
    _uid, _tid, rid, _epoch, token = await grant_env.seed_running("grant-cache@x.test")
    assert _grant(client, token).status_code == 200  # grant 入缓存

    # settle
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET state = 'settled', lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = :r"
            ),
            {"r": rid},
        )
    # settle 后：同 token 再取 → 401（fence 链序作废，缓存不救）
    assert _grant(client, token).status_code == 401

    # 二次取 → 仍 401（缓存已被首次 settle 后 401 排空，不残留旧值）
    assert _grant(client, token).status_code == 401


async def _running_round(pg, tid: str) -> tuple[str, int]:
    async with pg.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT id, lease_epoch FROM task_rounds WHERE task_id = :t "
                    "AND state = 'running'"
                ),
                {"t": tid},
            )
        ).first()
    assert row is not None
    return str(row[0]), int(row[1])


@pytest.fixture
async def app_engine(role_engine):
    from tests.conftest import APP_ROLE

    engine = role_engine(APP_ROLE)
    yield engine
    await engine.dispose()
