"""任务服务：complete / abort / delete 生命周期（Phase 6；Phase 9 T6 拆分）。

自 test_v2_task_service.py 逐字搬移；共享助手见 v2_task_service_helpers。
"""

import pytest
from sqlalchemy import text

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.task_service import (
    abort_task,
    complete_task,
    delete_task,
)
from tests import v2_task_service_helpers as _ts_helpers
from tests.v2_provider_helpers import seed_task_for_provider
from tests.v2_task_helpers import (
    owner_tx,
    seed_running_task,
)

# 夹具发现形态：以赋值别名引入（admin_env 同款惯例）。
app_engine = _ts_helpers.app_engine
domain = _ts_helpers.domain

_D14_VIEW_KEYS = {
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
_FAKE_CATALOG_ID = "00000000-0000-0000-0000-00000000000c"


async def test_complete_queued_direct_edge(pg, app_engine, domain):
    """契约 C1（Sup §1.2:30 + D19）：queued→completed 直接边——pending round→
    cancelled + completed + active 释放；task_root 与字节账保留 7 天（DB §5:177）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")  # active+task_root+pending 轮
    async with owner_tx(app_engine, uid) as db:
        out = await complete_task(db, owner_id=uid, task_id=tid)
    assert out["task"]["status"] == "completed"
    assert out["task"]["abort_reason"] is None
    assert out["task"]["active_round"] is None
    assert "round" not in out  # 200 形载荷（D14）
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        rnd = (
            await conn.execute(text("SELECT state FROM task_rounds WHERE task_id = :t"), {"t": tid})
        ).scalar_one()
        evs = (
            await conn.execute(
                text(
                    "SELECT type, sequence, payload_json FROM task_events WHERE task_id = :t "
                    "ORDER BY sequence"
                ),
                {"t": tid},
            )
        ).all()
        usage = (
            (
                await conn.execute(
                    text(
                        "SELECT active_tasks, running_tasks, retained_storage_bytes "
                        "FROM user_quota_usage WHERE user_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
    assert dict(res) == {"active": "released", "task_root": "held"}  # task_root 保留 7 天
    assert rnd == "cancelled"
    assert [e[0] for e in evs] == ["status_changed", "round_cancelled"]  # 种子 event_sequence=0
    assert [e[1] for e in evs] == [1, 2]
    assert evs[0][2] == {"status": "completed"}
    assert evs[1][2] == {}
    assert usage["active_tasks"] == 0 and usage["running_tasks"] == 0
    assert usage["retained_storage_bytes"] == 0


async def test_complete_running_intent_202(pg, app_engine, domain):
    """D19：running 的 complete → pending_terminal='completed' + 活跃轮→cancelling
    （202 形载荷）；状态/账目不动（轮收口事务裁定）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_running_task(pg, uid, pid)
    async with owner_tx(app_engine, uid) as db:
        out = await complete_task(db, owner_id=uid, task_id=tid)
    assert out["round"] == {"state": "cancelling"}  # 202 形载荷
    assert out["task"]["status"] == "running"
    async with pg.engine.connect() as conn:
        intent = (
            (
                await conn.execute(
                    text("SELECT pending_terminal, status FROM tasks WHERE id = :t"), {"t": tid}
                )
            )
            .mappings()
            .one()
        )
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        rnd = (
            (
                await conn.execute(
                    text("SELECT state, lease_owner FROM task_rounds WHERE task_id = :t"),
                    {"t": tid},
                )
            )
            .mappings()
            .one()
        )
        usage = (
            (
                await conn.execute(
                    text(
                        "SELECT active_tasks, running_tasks FROM user_quota_usage "
                        "WHERE user_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
        slot = (
            (
                await conn.execute(
                    text("SELECT state, task_id FROM platform_slots WHERE slot_no = 1")
                )
            )
            .mappings()
            .one()
        )
        n_evs = (
            await conn.execute(
                text("SELECT count(*) FROM task_events WHERE task_id = :t"), {"t": tid}
            )
        ).scalar_one()
    assert intent["pending_terminal"] == "completed" and intent["status"] == "running"
    assert dict(res) == {"active": "held", "running": "held", "task_root": "held"}
    assert rnd["state"] == "cancelling" and rnd["lease_owner"] == "exec-test"
    assert usage["active_tasks"] == 1 and usage["running_tasks"] == 1
    assert slot["state"] == "leased" and str(slot["task_id"]) == str(tid)
    assert n_evs == 0  # 意图分支不写事件（轮收口时补）


async def test_abort_running_without_round_returns_current_view(pg, app_engine, domain):
    """rowcount=0（无活跃轮=已收口竞态）→ 200 形当前视图不释放、不写意图位。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="running")  # 裸 running（无轮无账）
    async with owner_tx(app_engine, uid) as db:
        out = await abort_task(db, owner_id=uid, task_id=tid)
    assert "round" not in out
    assert out["task"]["status"] == "running"
    async with pg.engine.connect() as conn:
        intent = (
            await conn.execute(text("SELECT pending_terminal FROM tasks WHERE id = :t"), {"t": tid})
        ).scalar_one()
    assert intent is None


async def test_abort_ready_terminalizes_and_releases_active(pg, app_engine, domain):
    """ready→aborted(user_cancel)：active 释放 + active_tasks-1；task_root 保留
    7 天；settled 轮不受影响。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="ready")
    async with pg.engine.begin() as conn:  # 贴合真实生命周期：ready = 轮已 settled
        await conn.execute(
            text("UPDATE task_rounds SET state = 'settled' WHERE task_id = :t"), {"t": tid}
        )
    async with owner_tx(app_engine, uid) as db:
        out = await abort_task(db, owner_id=uid, task_id=tid)
    assert out["task"]["status"] == "aborted"
    assert out["task"]["abort_reason"] == "user_cancel"
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        evs = (
            await conn.execute(
                text("SELECT type, sequence, payload_json FROM task_events WHERE task_id = :t"),
                {"t": tid},
            )
        ).all()
        usage = (
            await conn.execute(
                text("SELECT active_tasks FROM user_quota_usage WHERE user_id = :u"), {"u": uid}
            )
        ).scalar_one()
    assert dict(res) == {"active": "released", "task_root": "held"}
    assert [e[0] for e in evs] == ["status_changed"]
    assert evs[0][2] == {"status": "aborted", "reason": "user_cancel"}
    assert usage == 0


async def test_abort_uploading_rejected_409(pg, app_engine, domain):
    """uploading→aborted 非法迁移（uploading 用 DELETE 终态化）→ TASK_INVALID_
    TRANSITION 409；任务与持有物原样。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await abort_task(db, owner_id=uid, task_id=tid)
    assert ei.value.code is ErrorCode.TASK_INVALID_TRANSITION
    assert ei.value.http_status == 409
    async with pg.engine.connect() as conn:
        status = (
            await conn.execute(text("SELECT status FROM tasks WHERE id = :t"), {"t": tid})
        ).scalar_one()
        res = (
            await conn.execute(
                text("SELECT state FROM task_reservations WHERE task_id = :t"), {"t": tid}
            )
        ).scalar_one()
    assert status == "uploading" and res == "held"


# ---------------------------------------------------------------------------
# delete：任意非 deleted → deleted（账面先行全清）
# ---------------------------------------------------------------------------


async def test_delete_running_full_clear(pg, app_engine, domain):
    """running 删除全清：轮 cancelling + pending_terminal='deleted'；三条 reservation
    全 released（RETURNING 闸门退账）；槽位 free；双计数归零；存储账全退。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_running_task(pg, uid, pid)
    async with pg.engine.begin() as conn:  # 预置字节账（task_root 700；双账同额）
        await conn.execute(
            text(
                "UPDATE task_reservations SET bytes = 700 WHERE task_id = :t AND kind = 'task_root'"
            ),
            {"t": tid},
        )
        await conn.execute(
            text("UPDATE user_quota_usage SET retained_storage_bytes = 700 WHERE user_id = :u"),
            {"u": uid},
        )
        await conn.execute(
            text("UPDATE platform_storage SET retained_storage_bytes = 700 WHERE singleton")
        )
    async with owner_tx(app_engine, uid) as db:
        out = await delete_task(db, owner_id=uid, task_id=tid)
    assert out["task"] == {"id": str(tid), "status": "deleted"}  # D14 delete 形状
    async with pg.engine.connect() as conn:
        task = (
            (
                await conn.execute(
                    text("SELECT status, pending_terminal FROM tasks WHERE id = :t"), {"t": tid}
                )
            )
            .mappings()
            .one()
        )
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        rnd = (
            await conn.execute(text("SELECT state FROM task_rounds WHERE task_id = :t"), {"t": tid})
        ).scalar_one()
        slot = (
            (
                await conn.execute(
                    text(
                        "SELECT state, task_id, leased_until FROM platform_slots WHERE slot_no = 1"
                    )
                )
            )
            .mappings()
            .one()
        )
        usage = (
            (
                await conn.execute(
                    text(
                        "SELECT active_tasks, running_tasks, retained_storage_bytes "
                        "FROM user_quota_usage WHERE user_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
        platform_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM platform_storage WHERE singleton")
            )
        ).scalar_one()
        evs = (
            await conn.execute(
                text("SELECT type, sequence FROM task_events WHERE task_id = :t ORDER BY sequence"),
                {"t": tid},
            )
        ).all()
    assert task["status"] == "deleted" and task["pending_terminal"] == "deleted"
    assert dict(res) == {"active": "released", "running": "released", "task_root": "released"}
    assert rnd == "cancelling"  # 活跃轮交轮收口作业收尾
    assert slot["state"] == "free" and slot["task_id"] is None and slot["leased_until"] is None
    assert usage["active_tasks"] == 0 and usage["running_tasks"] == 0
    assert usage["retained_storage_bytes"] == 0 and platform_bytes == 0
    assert [e[0] for e in evs] == ["status_changed"]
    assert evs[0][1] == 1


async def test_delete_terminal_refunds_storage(pg, app_engine, domain):
    """终态（非 deleted）任务删除：task_root/active 全 released + 存储账全退
    （deleted 即时全清 vs 终态 7 天保留的分档收口）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    async with pg.engine.begin() as conn:
        await conn.execute(text("UPDATE tasks SET status = 'completed' WHERE id = :t"), {"t": tid})
        await conn.execute(
            text("UPDATE task_rounds SET state = 'settled' WHERE task_id = :t"), {"t": tid}
        )
        await conn.execute(
            text(
                "UPDATE task_reservations SET bytes = 500 WHERE task_id = :t AND kind = 'task_root'"
            ),
            {"t": tid},
        )
        await conn.execute(
            text("UPDATE user_quota_usage SET retained_storage_bytes = 500 WHERE user_id = :u"),
            {"u": uid},
        )
        await conn.execute(
            text("UPDATE platform_storage SET retained_storage_bytes = 500 WHERE singleton")
        )
    async with owner_tx(app_engine, uid) as db:
        out = await delete_task(db, owner_id=uid, task_id=tid)
    assert out["task"] == {"id": str(tid), "status": "deleted"}
    async with pg.engine.connect() as conn:
        res = (
            await conn.execute(
                text("SELECT kind, state FROM task_reservations WHERE task_id = :t ORDER BY kind"),
                {"t": tid},
            )
        ).all()
        usage = (
            (
                await conn.execute(
                    text(
                        "SELECT active_tasks, retained_storage_bytes FROM user_quota_usage "
                        "WHERE user_id = :u"
                    ),
                    {"u": uid},
                )
            )
            .mappings()
            .one()
        )
        platform_bytes = (
            await conn.execute(
                text("SELECT retained_storage_bytes FROM platform_storage WHERE singleton")
            )
        ).scalar_one()
        evs = (
            await conn.execute(
                text("SELECT type, sequence FROM task_events WHERE task_id = :t ORDER BY sequence"),
                {"t": tid},
            )
        ).all()
    assert dict(res) == {"active": "released", "task_root": "released"}
    assert usage["active_tasks"] == 0 and usage["retained_storage_bytes"] == 0
    assert platform_bytes == 0
    assert [e[0] for e in evs] == ["status_changed"]  # settled 轮不补 round_cancelled


async def test_delete_twice_returns_not_found(pg, app_engine, domain):
    """Sup §1.2:31：DELETE 幂等重放由路由层承担；本服务层语义 = 已删除任务统一
    404（新请求）。"""
    uid, pid, _rid, _cid = domain
    tid = await seed_task_for_provider(pg, uid, pid, status="uploading")
    async with owner_tx(app_engine, uid) as db:
        out = await delete_task(db, owner_id=uid, task_id=tid)
    assert out["task"]["status"] == "deleted"
    with pytest.raises(AgentCraftError) as ei:
        async with owner_tx(app_engine, uid) as db:
            await delete_task(db, owner_id=uid, task_id=tid)
    assert ei.value.code is ErrorCode.TASK_NOT_FOUND
    assert ei.value.http_status == 404
