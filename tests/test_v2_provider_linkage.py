"""任务联动测试：fail_unstarted_tasks 批量终态化未开始任务（裁决 D3）。

tasks 属 owner-RLS 表：种子 superuser（helpers.seed_task_for_provider，status
参数化）；调用在 owner_session 事务内；owner_session with 退出即提交。
"""

from sqlalchemy import text

from backend.v2.provider_service import fail_unstarted_tasks
from backend.v2.runtime import owner_session
from tests.test_v2_runtime import make_v2_runtime
from tests.v2_provider_helpers import seed_active_user, seed_provider, seed_task_for_provider


async def test_fail_unstarted_targets_exactly_three_states(pg):
    """uploading/queued/ready → failed；running/completed/aborted 不动（D3）。"""
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "link-a@x.test")
        pid = await seed_provider(pg, uid)
        for status in ("uploading", "queued", "ready", "running", "completed", "aborted"):
            await seed_task_for_provider(pg, uid, pid, status=status)
        async with owner_session(rt, str(uid)) as db:
            n = await fail_unstarted_tasks(db, provider_id=str(pid))
        assert n == 3
        async with pg.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT status, abort_reason FROM tasks "
                        "WHERE provider_id = :p ORDER BY status"
                    ),
                    {"p": str(pid)},
                )
            ).all()
        changed = [r for r in rows if r[0] == "failed"]
        assert len(changed) == 3 and all(r[1] == "provider_key_revoked" for r in changed)
        assert {r[0] for r in rows} - {"failed"} == {"running", "completed", "aborted"}
    finally:
        rt.close()


async def test_fail_unstarted_releases_reservations_and_quota(pg):
    """对称释放（D3/DB Design §4.2:147）：held reservation → released；
    active_tasks 按释放的 active 条数递减归零（task_root 不计 active_tasks）。"""
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "link-e@x.test")
        pid = await seed_provider(pg, uid)
        await seed_task_for_provider(pg, uid, pid, status="uploading")  # 1 条 active
        await seed_task_for_provider(pg, uid, pid, status="queued")  # active + task_root
        async with owner_session(rt, str(uid)) as db:
            await fail_unstarted_tasks(db, provider_id=str(pid))
        async with pg.engine.connect() as conn:
            states = (
                await conn.execute(
                    text(
                        "SELECT kind, state FROM task_reservations r "
                        "JOIN tasks t ON t.id = r.task_id "
                        "WHERE t.provider_id = :p ORDER BY kind"
                    ),
                    {"p": str(pid)},
                )
            ).all()
            active_tasks = (
                await conn.execute(
                    text("SELECT active_tasks FROM user_quota_usage WHERE user_id = :u"),
                    {"u": uid},
                )
            ).scalar_one()
        assert all(state == "released" for _, state in states)
        assert {kind for kind, _ in states} == {"active", "task_root"}
        assert active_tasks == 0  # 2 个任务各递减 1（active 计数），非 3
    finally:
        rt.close()


async def test_fail_unstarted_cancels_pending_round(pg):
    """queued 任务的 pending round → cancelled + round_cancelled 事件（D3：防 Phase 6 僵尸轮）。"""
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "link-f@x.test")
        pid = await seed_provider(pg, uid)
        tid = await seed_task_for_provider(pg, uid, pid, status="queued")  # 含 pending round
        async with owner_session(rt, str(uid)) as db:
            await fail_unstarted_tasks(db, provider_id=str(pid))
        async with pg.engine.connect() as conn:
            round_state = (
                await conn.execute(
                    text("SELECT state FROM task_rounds WHERE task_id = :t"), {"t": tid}
                )
            ).scalar_one()
            events = (
                (
                    await conn.execute(
                        text("SELECT type FROM task_events WHERE task_id = :t ORDER BY sequence"),
                        {"t": tid},
                    )
                )
                .scalars()
                .all()
            )
            seq = (
                await conn.execute(
                    text("SELECT event_sequence FROM tasks WHERE id = :t"), {"t": tid}
                )
            ).scalar_one()
        assert round_state == "cancelled"
        assert list(events) == ["status_changed", "round_cancelled"]  # sequence 1、2
        assert seq == 2
    finally:
        rt.close()


async def test_fail_unstarted_writes_status_changed_events(pg):
    """逐任务补 task_events(status_changed)：sequence=event_sequence+1，counter 同步。"""
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "link-b@x.test")
        pid = await seed_provider(pg, uid)
        # event_sequence=0，无 round
        tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
        async with owner_session(rt, str(uid)) as db:
            await fail_unstarted_tasks(db, provider_id=str(pid))
        async with pg.engine.connect() as conn:
            ev = (
                (
                    await conn.execute(
                        text(
                            "SELECT type, sequence, payload_json "
                            "FROM task_events WHERE task_id = :t"
                        ),
                        {"t": tid},
                    )
                )
                .mappings()
                .one()
            )
            seq = (
                await conn.execute(
                    text("SELECT event_sequence FROM tasks WHERE id = :t"), {"t": tid}
                )
            ).scalar_one()
        assert ev["type"] == "status_changed" and ev["sequence"] == 1
        assert ev["payload_json"]["reason"] == "provider_key_revoked"
        assert seq == 1
    finally:
        rt.close()


async def test_fail_unstarted_idempotent(pg):
    """重复调用零增量（已 failed 不再命中；active_tasks 不二次递减）。"""
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "link-c@x.test")
        pid = await seed_provider(pg, uid)
        await seed_task_for_provider(pg, uid, pid, status="uploading")
        async with owner_session(rt, str(uid)) as db:
            first = await fail_unstarted_tasks(db, provider_id=str(pid))
            second = await fail_unstarted_tasks(db, provider_id=str(pid))
        assert (first, second) == (1, 0)
        async with pg.engine.connect() as conn:
            active_tasks = (
                await conn.execute(
                    text("SELECT active_tasks FROM user_quota_usage WHERE user_id = :u"),
                    {"u": uid},
                )
            ).scalar_one()
        assert active_tasks == 0  # 第二次调用未再递减
    finally:
        rt.close()


async def test_fail_unstarted_scoped_to_provider(pg):
    """只影响目标 Provider 的任务（他人/其它 Provider 任务不动）。"""
    rt = make_v2_runtime(pg)
    try:
        uid = await seed_active_user(pg, "link-d@x.test")
        pid_a = await seed_provider(pg, uid, model_id="gpt-4o-mini")
        pid_b = await seed_provider(pg, uid, model_id="gpt-4o")
        tid_b = await seed_task_for_provider(pg, uid, pid_b, status="queued")
        async with owner_session(rt, str(uid)) as db:
            await fail_unstarted_tasks(db, provider_id=str(pid_a))
        async with pg.engine.connect() as conn:
            status = (
                await conn.execute(text("SELECT status FROM tasks WHERE id = :t"), {"t": tid_b})
            ).scalar_one()
        assert status == "queued"
    finally:
        rt.close()
