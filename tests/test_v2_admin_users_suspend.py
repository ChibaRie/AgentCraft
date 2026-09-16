"""admin 用户 suspend/unsuspend（T3b 级联两段拆分，D3/D16）。

自 test_v2_admin_users.py 后半逐字搬移（Phase 9 T6 拆分）；共享助手见
v2_admin_helpers。
"""

import uuid as _uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import text

from backend.main import app
from backend.v2 import admin_user_service
from backend.v2.runtime import owner_session
from backend.v2.session_service import COOKIE_NAME, create_session
from tests import v2_admin_helpers as _vah
from tests.v2_admin_helpers import (
    _count,
    _grant,
    _one,
    _rewind_mfa_verified,
    _seed_user,
    _user_client,
    admin_client,
)
from tests.v2_provider_helpers import seed_provider, seed_task_for_provider
from tests.v2_task_helpers import seed_running_task, seed_task_user

_UA = "AgentCraft-AdminUsersTest/1.0"
_USERS = "/api/admin/users"

admin_env = _vah.admin_env

# ---------- T3b suspend/unsuspend（级联两段拆分，D3/D16）----------


class _FakeExecutor:
    """轻量假 executor（形态申报）：仅记录 stop_round 调用并回执 stopped=True；
    不触真实轮执行/容器——真件 bounded-stop（含 5s 超时路径）归
    test_v2_task_executor T6b 面，此处只钉级联对执行器的调用契约。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def stop_round(self, round_id: str, *, reason: str, timeout: float) -> dict:
        self.calls.append({"round_id": round_id, "reason": reason, "timeout": timeout})
        return {"round_id": round_id, "mode": "graceful", "stopped": True}


async def _seed_session(pg, rt, uid: str) -> str:
    """为目标用户种一个活跃会话（owner_session 单事务；revoke_all 生效实证的承载）。"""
    async with owner_session(rt, uid) as db:
        token, _csrf = await create_session(db, user_id=_uuid.UUID(uid), device_label=_UA)
    return token


async def _seed_action_token(pg, uid: str, purpose: str) -> str:
    """种一条未消费 account_action_tokens 行（token_hash 64 位唯一即可）。"""
    token_hash = (_uuid.uuid4().hex + _uuid.uuid4().hex)[:64]
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO account_action_tokens (id, user_id, purpose, token_hash, expires_at) "
                "VALUES (gen_random_uuid(), CAST(:u AS uuid), :p, :h, now() + interval '1 day')"
            ),
            {"u": uid, "p": purpose, "h": token_hash},
        )
    return token_hash


async def _set_status(pg, uid: str, status: str, *, deadline: bool = False) -> None:
    """superuser 直改用户状态（deleting 场景并置 deadline，供 C4 清列断言）。"""
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE users SET status = :s, "
                "deletion_deadline_at = CASE WHEN :d THEN now() + interval '14 days' "
                "ELSE deletion_deadline_at END "
                "WHERE id = CAST(:u AS uuid)"
            ),
            {"s": status, "u": uid, "d": deadline},
        )


async def _post_suspend(client, uid: str, *, reason: str = "违规处置", key: str = "k-s1"):
    return await client.post(
        f"{_USERS}/{uid}/suspend", json={"reason": reason}, headers={"Idempotency-Key": key}
    )


async def _post_unsuspend(client, uid: str, *, reason: str = "申诉通过恢复", key: str = "k-u1"):
    return await client.post(
        f"{_USERS}/{uid}/unsuspend", json={"reason": reason}, headers={"Idempotency-Key": key}
    )


async def test_suspend_full_chain_row_changes_audit_receipts_and_old_cookie_401(pg, admin_env):
    """suspend 全链行值真实变化：users.status=suspended；会话 revoked_at 非空 +
    旧 cookie 401（revoke_all 生效实证）；仅 deletion_cancel 令牌 consumed_at 置废
    （email_verify 不误伤）；entitlement 不撤销（revoke_entitlement=False）；
    审计 user.suspend（before_status/deadline 标记）；级联 receipts 并入响应。"""
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="susp@example.com")
    try:
        uid = await _seed_user(pg, "victim@example.com")
        stale_cookie = await _seed_session(pg, admin_env, uid)
        await _seed_action_token(pg, uid, "deletion_cancel")
        await _seed_action_token(pg, uid, "email_verify")
        assert (await _grant(client, uid, key="k-s-ent")).status_code == 201

        resp = await _post_suspend(client, uid)
        assert resp.status_code == 200
        assert resp.json()["data"] == {
            "user_id": uid,
            "before_status": "active",
            "status": "suspended",
            "deadline_cleared": False,
            "cascade": {
                "sessions_revoked": 1,
                "tokens_invalidated": 1,
                "flipped_tasks": 0,
                "cancelled_rounds": 0,
                "stopped": 0,
            },
        }
        row = await _one(
            pg,
            "SELECT status, deletion_deadline_at FROM users WHERE id = CAST(:u AS uuid)",
            {"u": uid},
        )
        assert row["status"] == "suspended" and row["deletion_deadline_at"] is None
        sess = await _one(
            pg, "SELECT revoked_at FROM sessions WHERE user_id = CAST(:u AS uuid)", {"u": uid}
        )
        assert sess["revoked_at"] is not None
        toks = await _one(
            pg,
            "SELECT count(*) FILTER (WHERE purpose = 'deletion_cancel' "
            "AND consumed_at IS NOT NULL) AS consumed, "
            "count(*) FILTER (WHERE purpose = 'email_verify' AND consumed_at IS NULL) "
            "AS untouched FROM account_action_tokens WHERE user_id = CAST(:u AS uuid)",
            {"u": uid},
        )
        assert toks["consumed"] == 1 and toks["untouched"] == 1
        # revoke_entitlement=False：entitlement 活跃行不受影响
        assert (
            await _count(
                pg,
                "user_entitlements",
                "user_id = CAST(:u AS uuid) AND revoked_at IS NULL",
                {"u": uid},
            )
            == 1
        )
        audit = await _one(
            pg,
            "SELECT actor_id, target_id, reason, detail FROM audit_logs "
            "WHERE action = 'user.suspend'",
        )
        assert str(audit["actor_id"]) == admin_id and str(audit["target_id"]) == uid
        assert audit["reason"] == "违规处置"
        assert audit["detail"] == {
            "user_id": uid,
            "before_status": "active",
            "deadline_cleared": False,
        }

        # 旧 cookie 401（revoke_all 生效实证）
        stale = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("10.7.0.9", 51003)),
            base_url="http://testserver",
            headers={"User-Agent": _UA},
            cookies={COOKIE_NAME: stale_cookie},
        )
        try:
            r = await stale.get("/api/auth/sessions")
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "SESSION_EXPIRED"
        finally:
            await stale.aclose()
    finally:
        await client.aclose()


async def test_suspend_deleting_user_clears_deadline_c4(pg, admin_env):
    """C4 单语句：deleting 用户 suspend 成功且同语句清 deadline（CHECK 约束自洽）；
    cancel token 同事务置废（封禁优先于撤销期，Sup:167）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="c4@example.com")
    try:
        uid = await _seed_user(pg, "leaver@example.com")
        await _set_status(pg, uid, "deleting", deadline=True)
        await _seed_action_token(pg, uid, "deletion_cancel")

        resp = await _post_suspend(client, uid, reason="注销期违规封禁", key="k-s-del")
        assert resp.status_code == 200
        assert resp.json()["data"]["before_status"] == "deleting"
        assert resp.json()["data"]["deadline_cleared"] is True
        row = await _one(
            pg,
            "SELECT status, deletion_deadline_at FROM users WHERE id = CAST(:u AS uuid)",
            {"u": uid},
        )
        assert row["status"] == "suspended" and row["deletion_deadline_at"] is None
        tok = await _one(
            pg,
            "SELECT consumed_at FROM account_action_tokens WHERE user_id = CAST(:u AS uuid)",
            {"u": uid},
        )
        assert tok["consumed_at"] is not None
        audit = await _one(pg, "SELECT detail FROM audit_logs WHERE action = 'user.suspend'")
        assert audit["detail"] == {
            "user_id": uid,
            "before_status": "deleting",
            "deadline_cleared": True,
        }
    finally:
        await client.aclose()


async def test_suspend_missing_404_pending_deleted_409_and_bad_uuid(pg, admin_env):
    """不存在 → 404；pending/deleted（行存在但状态非 active|deleting）→ 409
    USER_STATUS_CONFLICT；非 UUID → 400；失败路径零审计零副作用。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="s404@example.com")
    try:
        pending = await _seed_user(pg, "pending-v@example.com", status="pending")
        deleted = await _seed_user(pg, "gone-v@example.com", status="deleted")

        missing = await _post_suspend(client, str(_uuid.uuid4()))
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        bad = await _post_suspend(client, "not-a-uuid", key="k-s-bad")
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
        for uid, key in ((pending, "k-s-pend"), (deleted, "k-s-del409")):
            r = await _post_suspend(client, uid, key=key)
            assert r.status_code == 409, uid
            assert r.json()["error"]["code"] == "USER_STATUS_CONFLICT"
        assert await _count(pg, "audit_logs") == 0
        assert await _count(pg, "account_action_tokens") == 0
    finally:
        await client.aclose()


async def test_suspend_already_suspended_409_no_side_effects(pg, admin_env):
    """已 suspended → 409（原子谓词 rowcount=0）；已有会话不被级联误撤、零审计。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="again@example.com")
    try:
        uid = await _seed_user(pg, "already@example.com")
        await _set_status(pg, uid, "suspended")
        await _seed_session(pg, admin_env, uid)

        resp = await _post_suspend(client, uid)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "USER_STATUS_CONFLICT"
        sess = await _one(
            pg, "SELECT revoked_at FROM sessions WHERE user_id = CAST(:u AS uuid)", {"u": uid}
        )
        assert sess["revoked_at"] is None
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()


async def test_suspend_queued_task_aborted_round_cancelled_holdings_released(pg, admin_env):
    """queued 任务级联回收（D16 两段式）：任务 aborted(admin_suspended) +
    pending 轮 cancelled（清 lease）+ active reservation released + active_tasks
    递减；task_root 保留（aborted 档 7 天保留）；status_changed/round_cancelled
    事件按序落库；executor 未接线（None）时 stopped=0。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="qt@example.com")
    try:
        uid = await seed_task_user(pg, "queued-victim@example.com")
        pid = await seed_provider(pg, uid)
        tid = await seed_task_for_provider(pg, uid, pid, status="queued")

        resp = await _post_suspend(client, uid)
        assert resp.status_code == 200
        assert resp.json()["data"]["cascade"] == {
            "sessions_revoked": 0,
            "tokens_invalidated": 0,
            "flipped_tasks": 1,
            "cancelled_rounds": 1,
            "stopped": 0,
        }
        task = await _one(
            pg,
            "SELECT status, abort_reason, pending_terminal FROM tasks WHERE id = CAST(:t AS uuid)",
            {"t": tid},
        )
        assert task == {
            "status": "aborted",
            "abort_reason": "admin_suspended",
            "pending_terminal": None,
        }
        rnd = await _one(
            pg,
            "SELECT state, lease_owner, lease_expires_at FROM task_rounds "
            "WHERE task_id = CAST(:t AS uuid)",
            {"t": tid},
        )
        assert rnd == {"state": "cancelled", "lease_owner": None, "lease_expires_at": None}
        held = await _one(
            pg,
            "SELECT count(*) FILTER (WHERE kind = 'active' AND state = 'released') AS active_rel, "
            "count(*) FILTER (WHERE kind = 'task_root' AND state = 'held') AS root_kept "
            "FROM task_reservations WHERE task_id = CAST(:t AS uuid)",
            {"t": tid},
        )
        assert held == {"active_rel": 1, "root_kept": 1}
        usage = await _one(
            pg,
            "SELECT active_tasks, running_tasks FROM user_quota_usage "
            "WHERE user_id = CAST(:u AS uuid)",
            {"u": uid},
        )
        assert usage == {"active_tasks": 0, "running_tasks": 0}
        events = await _one(
            pg,
            "SELECT array_agg(type ORDER BY sequence) AS types, "
            "array_agg(payload_json->>'reason' ORDER BY sequence) AS reasons "
            "FROM task_events WHERE task_id = CAST(:t AS uuid)",
            {"t": tid},
        )
        assert events["types"] == ["status_changed", "round_cancelled"]
        assert events["reasons"] == ["admin_suspended", "admin_suspended"]
    finally:
        await client.aclose()


async def test_suspend_running_task_stop_round_invoked_and_state_flipped(pg, admin_env):
    """running 任务：先 executor.stop_round（reason=admin_suspended、timeout=5.0，
    注入假件记录调用），后 owner_session 条件翻转 aborted + running 轮 cancelled +
    三本账释放（running/active reservation released + 槽位 free + 两账归零）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rt@example.com")
    try:
        uid = await seed_task_user(pg, "running-victim@example.com")
        pid = await seed_provider(pg, uid)
        tid = await seed_running_task(pg, uid, pid)
        rid = (
            await _one(
                pg,
                "SELECT id::text AS rid FROM task_rounds WHERE task_id = CAST(:t AS uuid)",
                {"t": tid},
            )
        )["rid"]
        fake = _FakeExecutor()
        admin_env.executor = fake

        resp = await _post_suspend(client, uid)
        assert resp.status_code == 200
        assert resp.json()["data"]["cascade"] == {
            "sessions_revoked": 0,
            "tokens_invalidated": 0,
            "flipped_tasks": 1,
            "cancelled_rounds": 1,
            "stopped": 1,
        }
        assert fake.calls == [{"round_id": rid, "reason": "admin_suspended", "timeout": 5.0}]
        task = await _one(
            pg,
            "SELECT status, abort_reason, pending_terminal FROM tasks WHERE id = CAST(:t AS uuid)",
            {"t": tid},
        )
        assert task == {
            "status": "aborted",
            "abort_reason": "admin_suspended",
            "pending_terminal": None,
        }
        rnd = await _one(
            pg,
            "SELECT state, lease_owner FROM task_rounds WHERE id = CAST(:r AS uuid)",
            {"r": rid},
        )
        assert rnd == {"state": "cancelled", "lease_owner": None}
        acct = await _one(
            pg,
            "SELECT "
            "count(*) FILTER (WHERE kind = 'running' AND state = 'released') AS run_rel, "
            "count(*) FILTER (WHERE kind = 'active' AND state = 'released') AS act_rel, "
            "count(*) FILTER (WHERE kind = 'task_root' AND state = 'held') AS root_kept "
            "FROM task_reservations WHERE task_id = CAST(:t AS uuid)",
            {"t": tid},
        )
        assert acct == {"run_rel": 1, "act_rel": 1, "root_kept": 1}
        slot = await _one(
            pg,
            "SELECT state, task_id FROM platform_slots WHERE slot_no = 1",
        )
        assert slot == {"state": "free", "task_id": None}  # seed_running_task 租的是 1 号槽
        usage = await _one(
            pg,
            "SELECT active_tasks, running_tasks FROM user_quota_usage "
            "WHERE user_id = CAST(:u AS uuid)",
            {"u": uid},
        )
        assert usage == {"active_tasks": 0, "running_tasks": 0}
    finally:
        await client.aclose()
        admin_env.executor = None


async def test_suspend_scan_excludes_ready_and_terminal_tasks(pg, admin_env):
    """scan 谓词钉死 queued/running（brief 逐字）：ready 与终态任务不在圈定面——
    零翻转零释放零轮收口（receipts 全零）；T5 ban_author 复用同级联的依据钉点。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="scan@example.com")
    try:
        uid = await seed_task_user(pg, "scan-victim@example.com")
        pid = await seed_provider(pg, uid)
        ready_tid = await seed_task_for_provider(pg, uid, pid, status="ready")
        done_tid = await seed_task_for_provider(pg, uid, pid, status="completed")

        resp = await _post_suspend(client, uid)
        assert resp.status_code == 200
        assert resp.json()["data"]["cascade"] == {
            "sessions_revoked": 0,
            "tokens_invalidated": 0,
            "flipped_tasks": 0,
            "cancelled_rounds": 0,
            "stopped": 0,
        }
        ready = await _one(
            pg, "SELECT status FROM tasks WHERE id = CAST(:t AS uuid)", {"t": ready_tid}
        )
        assert ready["status"] == "ready"
        done = await _one(
            pg, "SELECT status FROM tasks WHERE id = CAST(:t AS uuid)", {"t": done_tid}
        )
        assert done["status"] == "completed"
        rnd = await _one(
            pg, "SELECT state FROM task_rounds WHERE task_id = CAST(:t AS uuid)", {"t": ready_tid}
        )
        assert rnd["state"] == "pending"  # ready 的活跃轮未被收口
    finally:
        await client.aclose()


async def test_suspend_running_task_executor_none_degrades_but_flips(pg, admin_env):
    """executor None 降级（D3）：stop_round 不调用（stopped=0），任务状态照翻、
    轮照取消（轮由 reclaim fence 兜底是降级语义背书，级联自身仍收口轮）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="rtn@example.com")
    try:
        uid = await seed_task_user(pg, "running-none@example.com")
        pid = await seed_provider(pg, uid)
        tid = await seed_running_task(pg, uid, pid)
        assert admin_env.executor is None

        resp = await _post_suspend(client, uid)
        assert resp.status_code == 200
        assert resp.json()["data"]["cascade"]["stopped"] == 0
        assert resp.json()["data"]["cascade"]["flipped_tasks"] == 1
        task = await _one(
            pg, "SELECT status, abort_reason FROM tasks WHERE id = CAST(:t AS uuid)", {"t": tid}
        )
        assert task == {"status": "aborted", "abort_reason": "admin_suspended"}
        rnd = await _one(
            pg, "SELECT state FROM task_rounds WHERE task_id = CAST(:t AS uuid)", {"t": tid}
        )
        assert rnd["state"] == "cancelled"
    finally:
        await client.aclose()


async def test_cascade_skipped_when_admin_tx_fails(pg, admin_env, monkeypatch):
    """级联 post-commit 序断言：admin 事务提交失败（原子写已 flush 后抛错回滚）→
    级联不执行——会话未撤、令牌未消费、用户状态未翻转、零审计。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="ord@example.com")
    try:
        uid = await _seed_user(pg, "order@example.com")
        await _seed_session(pg, admin_env, uid)
        await _seed_action_token(pg, uid, "deletion_cancel")

        real = admin_user_service.suspend_user

        async def _boom(db, *args, **kwargs):
            await real(db, *args, **kwargs)  # 原子翻转 + 审计已在事务内 flush
            raise HTTPException(status_code=500, detail={"code": "TX_BOOM", "message": "boom"})

        monkeypatch.setattr(admin_user_service, "suspend_user", _boom)
        resp = await _post_suspend(client, uid)
        assert resp.status_code == 500

        row = await _one(pg, "SELECT status FROM users WHERE id = CAST(:u AS uuid)", {"u": uid})
        assert row["status"] == "active"  # 事务回滚，原子部分未落
        sess = await _one(
            pg, "SELECT revoked_at FROM sessions WHERE user_id = CAST(:u AS uuid)", {"u": uid}
        )
        assert sess["revoked_at"] is None  # 级联未执行（post-commit 序）
        tok = await _one(
            pg,
            "SELECT consumed_at FROM account_action_tokens WHERE user_id = CAST(:u AS uuid)",
            {"u": uid},
        )
        assert tok["consumed_at"] is None
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()


async def test_cascade_retry_service_level_idempotent(pg, admin_env):
    """级联重试幂等（服务层直调二次）：首轮 receipts 全命中，二轮全零（各段
    条件 UPDATE/谓词天然幂等，零新增副作用——D3「重试幂等自愈」的固定点语义）。"""
    uid = await seed_task_user(pg, "retry@example.com")
    pid = await seed_provider(pg, uid)
    await seed_task_for_provider(pg, uid, pid, status="queued")
    await _seed_session(pg, admin_env, uid)
    await _seed_action_token(pg, uid, "deletion_cancel")

    first = await admin_user_service.run_suspension_cascade(
        admin_env, target_user_id=uid, executor=None
    )
    assert first == {
        "sessions_revoked": 1,
        "tokens_invalidated": 1,
        "flipped_tasks": 1,
        "cancelled_rounds": 1,
        "stopped": 0,
    }
    second = await admin_user_service.run_suspension_cascade(
        admin_env, target_user_id=uid, executor=None
    )
    assert second == {
        "sessions_revoked": 0,
        "tokens_invalidated": 0,
        "flipped_tasks": 0,
        "cancelled_rounds": 0,
        "stopped": 0,
    }
    # 无双重效果：会话仍只撤一次、任务仍只翻转一次
    assert (
        await _count(
            pg, "sessions", "user_id = CAST(:u AS uuid) AND revoked_at IS NOT NULL", {"u": uid}
        )
        == 1
    )
    assert (
        await _count(
            pg,
            "task_events",
            "task_id = (SELECT id FROM tasks WHERE owner_id = CAST(:u AS uuid))",
            {"u": uid},
        )
        == 2
    )


async def test_suspend_idempotent_replay_returns_stored_payload(pg, admin_env):
    """同 key 重放原 store 载荷（幂等命中先于状态门，已 suspended 不 409）；
    级联 receipts 是提交后并入——store 载荷的 cascade=null 随重放原样返回
    （申报：重放不重跑级联，窗口自愈走服务层级联重试，见 D3 已知代价登记）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="replay@example.com")
    try:
        uid = await _seed_user(pg, "replayee@example.com")
        await _seed_session(pg, admin_env, uid)

        first = await _post_suspend(client, uid, key="k-s-replay")
        assert first.status_code == 200
        assert first.json()["data"]["cascade"]["sessions_revoked"] == 1
        replay = await _post_suspend(client, uid, key="k-s-replay")
        assert replay.status_code == 200
        body = replay.json()["data"]
        assert body["user_id"] == uid and body["status"] == "suspended"
        assert body["before_status"] == "active" and body["deadline_cleared"] is False
        assert body["cascade"] is None
        assert await _count(pg, "audit_logs", "action = 'user.suspend'") == 1  # 重放零新副作用
        assert (
            await _count(
                pg, "sessions", "user_id = CAST(:u AS uuid) AND revoked_at IS NOT NULL", {"u": uid}
            )
            == 1
        )
    finally:
        await client.aclose()


async def test_unsuspend_success_row_audit(pg, admin_env):
    """unsuspend：suspended→active 条件 UPDATE（行值真实变化）+ 审计
    user.unsuspend（before_status=suspended）；无级联段。"""
    client, _csrf, admin_id = await admin_client(pg, admin_env, email="uns@example.com")
    try:
        uid = await _seed_user(pg, "banned@example.com")
        await _set_status(pg, uid, "suspended")

        resp = await _post_unsuspend(client, uid)
        assert resp.status_code == 200
        assert resp.json()["data"] == {
            "user_id": uid,
            "before_status": "suspended",
            "status": "active",
        }
        row = await _one(pg, "SELECT status FROM users WHERE id = CAST(:u AS uuid)", {"u": uid})
        assert row["status"] == "active"
        audit = await _one(
            pg,
            "SELECT actor_id, target_id, detail FROM audit_logs WHERE action = 'user.unsuspend'",
        )
        assert str(audit["actor_id"]) == admin_id and str(audit["target_id"]) == uid
        assert audit["detail"] == {"user_id": uid, "before_status": "suspended"}
    finally:
        await client.aclose()


async def test_unsuspend_missing_404_non_suspended_409(pg, admin_env):
    """unsuspend：不存在 → 404；active/pending/deleting → 409；非 UUID → 400；
    零审计。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="uns409@example.com")
    try:
        active = await _seed_user(pg, "still-active@example.com")
        pending = await _seed_user(pg, "still-pending@example.com", status="pending")
        deleting = await _seed_user(pg, "still-deleting@example.com", status="deleting")

        missing = await _post_unsuspend(client, str(_uuid.uuid4()))
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"
        bad = await _post_unsuspend(client, "not-a-uuid", key="k-u-bad")
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "VALIDATION_ERROR"
        for uid, key in ((active, "k-u-a"), (pending, "k-u-p"), (deleting, "k-u-d")):
            r = await _post_unsuspend(client, uid, key=key)
            assert r.status_code == 409, uid
            assert r.json()["error"]["code"] == "USER_STATUS_CONFLICT"
        assert await _count(pg, "audit_logs") == 0
    finally:
        await client.aclose()


async def test_unsuspend_idempotent_replay(pg, admin_env):
    """unsuspend 同 key 同载荷原样重放（200 同响应体，零新审计）。"""
    client, _csrf, _admin_id = await admin_client(pg, admin_env, email="unsreplay@example.com")
    try:
        uid = await _seed_user(pg, "uns-replay@example.com")
        await _set_status(pg, uid, "suspended")

        first = await _post_unsuspend(client, uid, key="k-u-replay")
        assert first.status_code == 200
        replay = await _post_unsuspend(client, uid, key="k-u-replay")
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert await _count(pg, "audit_logs", "action = 'user.unsuspend'") == 1
    finally:
        await client.aclose()


async def test_suspend_unsuspend_gates_reason_idem_and_extra_forbid(pg, admin_env):
    """门负例：非 admin 双端点 403 FORBIDDEN；reason 缺失 400 ADMIN_REASON_REQUIRED；
    缺 Idempotency-Key 400；未知载荷字段（extra=forbid）400 VALIDATION_ERROR——
    含 T3a 审查顺延的 EntitlementPayload extra=forbid 回归；MFA 过期 403。"""
    plain = await _user_client(pg, admin_env, email="plain2@example.com")
    try:
        victim = str(_uuid.uuid4())
        s = await plain.post(
            f"{_USERS}/{victim}/suspend", json={"reason": "r"}, headers={"Idempotency-Key": "k-g-s"}
        )
        assert s.status_code == 403 and s.json()["error"]["code"] == "FORBIDDEN"
        u = await plain.post(
            f"{_USERS}/{victim}/unsuspend",
            json={"reason": "r"},
            headers={"Idempotency-Key": "k-g-u"},
        )
        assert u.status_code == 403 and u.json()["error"]["code"] == "FORBIDDEN"
    finally:
        await plain.aclose()

    client, _csrf, admin_id = await admin_client(pg, admin_env, email="gates@example.com")
    try:
        uid = await _seed_user(pg, "gated@example.com")
        no_reason = await client.post(
            f"{_USERS}/{uid}/suspend", json={}, headers={"Idempotency-Key": "k-g-nr"}
        )
        assert no_reason.status_code == 400
        assert no_reason.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        no_reason_u = await client.post(
            f"{_USERS}/{uid}/unsuspend", json={}, headers={"Idempotency-Key": "k-g-nru"}
        )
        assert no_reason_u.status_code == 400
        assert no_reason_u.json()["error"]["code"] == "ADMIN_REASON_REQUIRED"
        no_key = await client.post(f"{_USERS}/{uid}/suspend", json={"reason": "r"})
        assert no_key.status_code == 400
        assert no_key.json()["error"]["code"] == "VALIDATION_ERROR"
        extra = await client.post(
            f"{_USERS}/{uid}/suspend",
            json={"reason": "r", "bogus": 1},
            headers={"Idempotency-Key": "k-g-extra"},
        )
        assert extra.status_code == 400
        assert extra.json()["error"]["code"] == "VALIDATION_ERROR"
        # T3a 审查顺延回归：EntitlementPayload extra=forbid
        extra_ent = await client.post(
            f"{_USERS}/{uid}/entitlements",
            json={"kind": "expert_author", "reason": "r", "bogus": 1},
            headers={"Idempotency-Key": "k-g-extra-ent"},
        )
        assert extra_ent.status_code == 400
        assert extra_ent.json()["error"]["code"] == "VALIDATION_ERROR"

        await _rewind_mfa_verified(pg, admin_id, hours=13)
        stale = await _post_suspend(client, uid, key="k-g-stale")
        assert stale.status_code == 403
        assert stale.json()["error"]["code"] == "ADMIN_MFA_REQUIRED"

        assert await _count(pg, "audit_logs") == 0
        assert await _count(pg, "user_entitlements") == 0
    finally:
        await client.aclose()
