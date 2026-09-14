"""任务域 dispatcher 与四个后台作业（Phase 6 T5）——两段式领槽/回收/双清扫。

契约出处：task-5-brief 接口冻结 + Phase 6 计划 D16/D7f + DB Design §5:156/§5:177
（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）。模块纪律：

- **D16 两段式会话编排**：一切跨 owner 系统写路径分两段——阶段 1 admin 只读会话
  （``runtime.admin_factory()``，plain SELECT **无 FOR UPDATE**：RLS 表锁定读需
  SELECT+UPDATE 双 policy，admin 只有 SELECT，带锁即静默 0 行）圈定候选
  (round_id, task_id, owner_id)；阶段 2 逐候选 ``owner_session(owner_id)`` 单任务
  单事务：条件 UPDATE 仲裁（rowcount=0 弃该候选——``_CandidateDropped`` 触发整体
  回滚，不留半账）+ 无 RLS 表（platform_slots/task_reservations/user_quota_usage）
  同会话直写 + 事件 sequence 原子分配。跨候选循环在事务外层。
- **dispatch 空 churn 闸**：本阶段运行时无 executor 句柄（T6a 才注入）——
  ``dispatch_once`` 直接返回 0 不圈定不领取，防 dispatcher 循环空转。
- **写侧围栏（outbox 范本）**：reclaim 的翻转全部携带
  ``WHERE state=:旧值 AND lease_owner=:旧值 AND lease_epoch=:旧值``——阶段 1 快照
  与阶段 2 落库之间的并发翻转（用户 abort/并发 reclaim）使 rowcount=0 静默弃权；
  对任务行已消失（D18 物理删）同样静默弃权。
- **lease_epoch 单调**：reclaim 置回 pending 时清 lease_owner/lease_expires_at 但
  **保留 lease_epoch**（复位为 0 会让 epoch 复用——旧 epoch 令牌在重领后重新通过
  D17 fence，清两元组保计数器是安全读法）；attempt 同样不动（重领时 claim 再 +1）。
- **requeue 的任务面**：置回 pending 的轮所属任务条件 UPDATE running→queued——
  dispatch 阶段 1 只 JOIN tasks(status='queued')，不翻任务则回收轮永不可达；
  系统路径不走 assert_transition（task_state.py 纪律），条件 UPDATE 仲裁。
- **7 天清理顺序**：events → rounds → messages——task_rounds.source_message_id
  是 RESTRICT FK，轮行不先删则消息删不动；task_events.round_id 为弱引用无 FK。
  退还以残余 held→released 的 ``RETURNING bytes`` 汇总为闸门（零行即零退，幂等
  不二次退账）；物理删 task-storage 在事务 post-commit（账面先行，幂等 rmtree，
  失败留下一轮兜底）。
- 测试必须经 make_v2_runtime 真实双 role（D16 意义即防「测试全绿生产空转」）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid as _uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.v2.ids import uuid7
from backend.v2.models import TaskReservation
from backend.v2.runtime import V2Runtime, owner_session
from backend.v2.task_release import (
    _apply_release_accounting,
    _release_reservations,
    release_task_holdings,
)
from backend.v2.task_service import _add_event, _allocate_event_sequence

logger = logging.getLogger("agentcraft.task.dispatcher")

# D7f：attempt 达 3 的过期轮不再重试——直接 failed(round_failed)（重领 claim 会 +1，
# 故 3 次失败即 3 次领取）
_ATTEMPT_LIMIT = 3

_TERMINAL_STATUSES = ("completed", "failed", "aborted", "deleted")


class _CandidateDropped(Exception):
    """阶段 2 候选仲裁失败（rowcount=0）：弃该候选、整体回滚，不影响其余候选。"""


def _executor(runtime: V2Runtime):
    """运行时 executor 句柄（T6a 注入）；本阶段 V2Runtime 无该字段 → None。"""
    return getattr(runtime, "executor", None)


def _instance_id() -> str:
    """租约持有者标识（lease_owner 列 String(100)）：进程级唯一即可区分并发实例。"""
    return f"task-dispatch-{os.getpid()}"


# ---------------------------------------------------------------------------
# dispatch_once：pending 轮领槽（executor 存在时）
# ---------------------------------------------------------------------------

# 阶段 1：admin 只读圈定（plain SELECT 无锁；queued 任务的首轮按 created_at FIFO）
_DISPATCH_CANDIDATES_SQL = text(
    "SELECT r.id AS round_id, t.id AS task_id, t.owner_id AS owner_id "
    "FROM task_rounds r JOIN tasks t ON t.id = r.task_id "
    "WHERE r.state = 'pending' AND t.status = 'queued' "
    "ORDER BY r.created_at, r.id "
    "LIMIT :batch"
)

# 阶段 2-1：轮领取围栏（state='pending' 仲裁；lease 三元组 + attempt 单点 +1）
_CLAIM_ROUND_SQL = text(
    "UPDATE task_rounds SET state = 'running', lease_owner = :iid, "
    "lease_epoch = lease_epoch + 1, "
    "lease_expires_at = now() + make_interval(secs => :ttl), "
    "attempt = attempt + 1 "
    "WHERE id = :rid AND state = 'pending' "
    "RETURNING attempt"
)

# 阶段 2-2：任务 queued→running（条件仲裁；行锁同时为事件序号分配铺路）
_RUN_TASK_SQL = text("UPDATE tasks SET status = 'running' WHERE id = :tid AND status = 'queued'")

# 阶段 2-4：running 配额条件增（rowcount=0 → 超限弃候选；与 user_quotas JOIN 免预读）
_RUNNING_INC_SQL = text(
    "UPDATE user_quota_usage SET running_tasks = running_tasks + 1 "
    "WHERE user_id = :u AND running_tasks < "
    "(SELECT max_running_tasks FROM user_quotas WHERE user_id = :u)"
)

# 阶段 2-5：单槽位认领（子查询 SKIP LOCKED 防多实例双占；无 RLS 表直写）
_SLOT_LEASE_SQL = text(
    "UPDATE platform_slots SET state = 'leased', task_id = :t, "
    "leased_until = now() + make_interval(secs => :ttl) "
    "WHERE slot_no = ("
    "  SELECT slot_no FROM platform_slots WHERE state = 'free' "
    "  ORDER BY slot_no LIMIT 1 FOR UPDATE SKIP LOCKED"
    ") "
    "RETURNING slot_no"
)


async def dispatch_once(runtime: V2Runtime) -> int:
    """领取一批 pending 轮（置 running + 租约 + 槽位 + 配额 + 双事件），返回领取数。

    运行时无 executor 句柄（T6a 前）→ 直接返回 0，不圈定不领取（防空转 churn）。
    逐候选提交后调 ``runtime.executor.notify(task_id)``——notify 失败只记录不回滚
    （领取已落库成立，由 reclaim 的 lease 过期兜底；与 outbox at-least-once 同型）。
    """
    executor = _executor(runtime)
    if executor is None:
        return 0
    async with runtime.admin_factory() as session:
        candidates = (
            await session.execute(
                _DISPATCH_CANDIDATES_SQL,
                {"batch": get_settings().V2_TASK.dispatch_batch},
            )
        ).all()

    claimed = 0
    for round_id, task_id, owner_id in candidates:
        try:
            async with owner_session(runtime, str(owner_id)) as db:
                await _claim_round(
                    db,
                    round_id=round_id,
                    task_id=task_id,
                    owner_id=owner_id,
                    instance_id=_instance_id(),
                    ttl_seconds=get_settings().V2_TASK.lease_ttl_seconds,
                )
        except _CandidateDropped:
            continue  # 并发仲裁败者/配额满/无空闲槽：分文不写，轮留待下一轮
        claimed += 1
        try:
            await executor.notify(str(task_id))
        except Exception:
            logger.exception("executor notify failed (claim stands): task_id=%s", task_id)
    return claimed


async def _claim_round(
    db: AsyncSession,
    *,
    round_id: _uuid.UUID,
    task_id: _uuid.UUID,
    owner_id: _uuid.UUID,
    instance_id: str,
    ttl_seconds: int,
) -> None:
    """单候选领取事务体（调用方 owner_session 已设 GUC）：任一步 rowcount=0 即弃。"""
    flipped = await db.execute(
        _CLAIM_ROUND_SQL, {"rid": round_id, "iid": instance_id, "ttl": ttl_seconds}
    )
    if flipped.rowcount == 0:
        raise _CandidateDropped()  # 并发已领取/已取消
    attempt = int(flipped.scalar_one())
    if (await db.execute(_RUN_TASK_SQL, {"tid": task_id})).rowcount == 0:
        raise _CandidateDropped()  # 任务已非 queued（并发终态化/删除）
    db.add(
        TaskReservation(
            id=uuid7(),
            task_id=task_id,
            user_id=owner_id,
            kind="running",
            bytes=0,
            state="held",
        )
    )
    if (await db.execute(_RUNNING_INC_SQL, {"u": owner_id})).rowcount == 0:
        raise _CandidateDropped()  # running 配额满
    if (await db.execute(_SLOT_LEASE_SQL, {"t": task_id, "ttl": ttl_seconds})).rowcount == 0:
        raise _CandidateDropped()  # 无空闲槽位
    seq = await _allocate_event_sequence(db, task_id, 2)
    _add_event(
        db,
        task_id=task_id,
        owner_id=owner_id,
        sequence=seq,
        event_type="status_changed",
        payload={"status": "running"},
    )
    _add_event(
        db,
        task_id=task_id,
        owner_id=owner_id,
        sequence=seq + 1,
        event_type="round_running",
        payload={"round_id": str(round_id), "attempt": int(attempt)},
        round_id=round_id,
    )


# ---------------------------------------------------------------------------
# reclaim_once：cancelling 轮收口 + 过期 lease 轮回收（崩溃恢复）
# ---------------------------------------------------------------------------

# 阶段 1：admin 只读圈定——cancelling 轮 + 只认 round.lease_expires_at 过期的 running 轮
_RECLAIM_CANDIDATES_SQL = text(
    "SELECT r.id AS round_id, r.task_id AS task_id, r.owner_id AS owner_id, "
    "r.state AS state, r.lease_owner AS lease_owner, r.lease_epoch AS lease_epoch "
    "FROM task_rounds r "
    "WHERE r.state = 'cancelling' "
    "   OR (r.state = 'running' AND r.lease_expires_at IS NOT NULL "
    "       AND r.lease_expires_at < now()) "
    "ORDER BY r.created_at, r.id"
)

# 过期轮围栏翻转第一步：验围栏 + 清 owner/expires 两元组（epoch 保留，见模块纪律）
# 并以 RETURNING attempt 定分档（<limit 置回 pending 重试；>=limit 置 failed）
_FENCE_EXPIRED_SQL = text(
    "UPDATE task_rounds SET lease_owner = NULL, lease_expires_at = NULL "
    "WHERE id = :rid AND state = 'running' "
    "AND lease_owner = :lo AND lease_epoch = :le "
    "RETURNING attempt"
)

_REQUEUE_ROUND_SQL = text(
    "UPDATE task_rounds SET state = 'pending' WHERE id = :rid AND lease_owner IS NULL"
)
_FAIL_ROUND_SQL = text(
    "UPDATE task_rounds SET state = 'failed' WHERE id = :rid AND lease_owner IS NULL"
)
_REQUEUE_TASK_SQL = text(
    "UPDATE tasks SET status = 'queued' WHERE id = :tid AND status = 'running'"
)
_FAIL_TASK_SQL = text(
    "UPDATE tasks SET status = 'failed', abort_reason = 'round_failed' "
    "WHERE id = :tid AND status = 'running'"
)
_CANCEL_ROUND_SQL = text(
    "UPDATE task_rounds SET state = 'cancelled', lease_owner = NULL, "
    "lease_expires_at = NULL "
    "WHERE id = :rid AND state = 'cancelling' "
    "RETURNING attempt"
)


async def reclaim_once(runtime: V2Runtime) -> int:
    """回收作业：cancelling 轮（容器确认已死后幂等 cancelled）+ lease 过期 running 轮。

    - cancelling 轮：需 executor（T6a）``container_alive`` 确认容器已死才收口——
      本阶段无 executor → 跳过（cancelling 只可能来自 abort API 写位，任务保持
      running + pending_terminal，T6a settle/termination 接手终态化）；
    - 过期 running 轮：写侧围栏（lease_owner/lease_epoch 双谓词）→ attempt<3 置回
      pending + 任务 running→queued + 轮账释放（槽位 free + running_tasks-1）；
      attempt>=3 置 failed(round_failed) + 任务 failed + 全账释放。
    """
    async with runtime.admin_factory() as session:
        candidates = (await session.execute(_RECLAIM_CANDIDATES_SQL)).all()

    reclaimed = 0
    for round_id, task_id, owner_id, state, lease_owner, lease_epoch in candidates:
        if state == "cancelling":
            executor = _executor(runtime)
            if executor is None:
                logger.debug(
                    "cancelling round awaiting executor: task_id=%s round_id=%s",
                    task_id,
                    round_id,
                )
                continue
            try:
                alive = await executor.container_alive(str(task_id))
            except Exception:
                logger.exception("container_alive probe failed (skip): task_id=%s", task_id)
                continue
            if alive:
                continue  # 容器仍在：等 executor 正常收口，不与 settle 竞写
            try:
                async with owner_session(runtime, str(owner_id)) as db:
                    await _cancel_cancelling_round(
                        db, round_id=round_id, task_id=task_id, owner_id=owner_id
                    )
            except _CandidateDropped:
                continue
            reclaimed += 1
            continue
        try:
            async with owner_session(runtime, str(owner_id)) as db:
                await _reclaim_expired_round(
                    db,
                    round_id=round_id,
                    task_id=task_id,
                    owner_id=owner_id,
                    lease_owner=lease_owner,
                    lease_epoch=int(lease_epoch),
                )
        except _CandidateDropped:
            continue
        reclaimed += 1
    return reclaimed


async def _reclaim_expired_round(
    db: AsyncSession,
    *,
    round_id: _uuid.UUID,
    task_id: _uuid.UUID,
    owner_id: _uuid.UUID,
    lease_owner: str | None,
    lease_epoch: int,
) -> None:
    """过期 lease 轮回收（围栏仲裁 → 按 attempt 分档 pending/failed + 对称释放）。"""
    fenced = await db.execute(
        _FENCE_EXPIRED_SQL, {"rid": round_id, "lo": lease_owner, "le": lease_epoch}
    )
    if fenced.rowcount == 0:
        raise _CandidateDropped()  # 并发 reclaim 胜者已清租约 / 轮已翻转或消失
    attempt = int(fenced.scalar_one())
    to_failed = attempt >= _ATTEMPT_LIMIT
    step2 = await db.execute(
        _FAIL_ROUND_SQL if to_failed else _REQUEUE_ROUND_SQL, {"rid": round_id}
    )
    if step2.rowcount == 0:  # 行锁在握，理论不可达——条件仲裁双保险
        raise _CandidateDropped()
    task_flip = await db.execute(
        _FAIL_TASK_SQL if to_failed else _REQUEUE_TASK_SQL, {"tid": task_id}
    )
    if task_flip.rowcount == 0:
        # 任务已非 running（并发终态化/物理删）：分文不写弃候选，由该路径收口本轮
        raise _CandidateDropped()
    seq = await _allocate_event_sequence(db, task_id, 2)
    _add_event(
        db,
        task_id=task_id,
        owner_id=owner_id,
        sequence=seq,
        event_type="status_changed",
        payload={"status": "failed", "reason": "round_failed"}
        if to_failed
        else {"status": "queued"},
    )
    _add_event(
        db,
        task_id=task_id,
        owner_id=owner_id,
        sequence=seq + 1,
        event_type="round_failed" if to_failed else "round_queued",
        payload={"round_id": str(round_id), "attempt": attempt},
        round_id=round_id,
    )
    # 任务面已落位：release_task_holdings 按 status 分档——queued 只回收轮账（槽位
    # free + running_tasks-1）；failed 为终态另释放 active（task_root 保留 7 天）
    await release_task_holdings(db, task_id=str(task_id), owner_id=str(owner_id))


async def _cancel_cancelling_round(
    db: AsyncSession, *, round_id: _uuid.UUID, task_id: _uuid.UUID, owner_id: _uuid.UUID
) -> None:
    """cancelling 轮幂等收口（容器确认已死）：round→cancelled + 轮账释放。

    任务保持 running + pending_terminal（终态化属 T6a settle/termination 面——
    本作业只对轮负责，与 settle 竞写面隔离）。
    """
    flipped = await db.execute(_CANCEL_ROUND_SQL, {"rid": round_id})
    if flipped.rowcount == 0:
        raise _CandidateDropped()  # 并发已收口/轮已消失
    seq = await _allocate_event_sequence(db, task_id)
    _add_event(
        db,
        task_id=task_id,
        owner_id=owner_id,
        sequence=seq,
        event_type="round_cancelled",
        payload={"round_id": str(round_id)},
        round_id=round_id,
    )
    await release_task_holdings(db, task_id=str(task_id), owner_id=str(owner_id))


# ---------------------------------------------------------------------------
# sweep_upload_ttl：uploading 24h 未提交输入 → failed(upload_expired)
# ---------------------------------------------------------------------------

_UPLOAD_CANDIDATES_SQL = text(
    "SELECT id, owner_id FROM tasks "
    "WHERE status = 'uploading' AND created_at < now() - make_interval(hours => :h) "
    "ORDER BY created_at, id"
)

_UPLOAD_FAIL_SQL = text(
    "UPDATE tasks SET status = 'failed', abort_reason = 'upload_expired' "
    "WHERE id = :tid AND status = 'uploading'"
)


async def sweep_upload_ttl(runtime: V2Runtime) -> int:
    """uploading 超时清扫：created_at 超 upload_ttl_hours 未冻结输入的任务置
    failed(upload_expired) + active 释放 + active_tasks-1（task_root 未建，字节账
    不涉及）。返回本轮翻转的任务数。"""
    async with runtime.admin_factory() as session:
        candidates = (
            await session.execute(
                _UPLOAD_CANDIDATES_SQL,
                {"h": get_settings().V2_TASK.upload_ttl_hours},
            )
        ).all()

    processed = 0
    for task_id, owner_id in candidates:
        try:
            async with owner_session(runtime, str(owner_id)) as db:
                if (await db.execute(_UPLOAD_FAIL_SQL, {"tid": task_id})).rowcount == 0:
                    raise _CandidateDropped()  # 并发已提交/删除
                seq = await _allocate_event_sequence(db, task_id)
                _add_event(
                    db,
                    task_id=task_id,
                    owner_id=owner_id,
                    sequence=seq,
                    event_type="status_changed",
                    payload={"status": "failed", "reason": "upload_expired"},
                )
                await release_task_holdings(db, task_id=str(task_id), owner_id=str(owner_id))
        except _CandidateDropped:
            continue
        processed += 1
    return processed


# ---------------------------------------------------------------------------
# sweep_terminal_cleanup：终态 7 天后清 events/rounds/messages + 残余账退 + 物理删
# ---------------------------------------------------------------------------

_TERMINAL_CANDIDATES_SQL = text(
    "SELECT id, owner_id FROM tasks "
    "WHERE status IN ('completed','failed','aborted','deleted') "
    "AND created_at < now() - make_interval(days => :d) "
    "ORDER BY created_at, id"
)


async def sweep_terminal_cleanup(runtime: V2Runtime) -> int:
    """终态保留期（terminal_retention_days=7）清理：events/rounds/messages 删除 +
    残余 held reservation 释放（``RETURNING bytes`` 闸门退账，零行即零退）+
    post-commit 物理删 task-storage（幂等）。

    返回**有清理动作**的任务数（行删除或账退还至少其一）：无动作的重扫不计——
    物理删失败的任务行仍在圈定面内，下一轮继续兜底重试，不产生日志噪音。
    task_files 行保留（行是产物登记事实源，物理树已删由读路径 404 兜底；D20 裁决
    见 task-5-report）。
    """
    async with runtime.admin_factory() as session:
        candidates = (
            await session.execute(
                _TERMINAL_CANDIDATES_SQL,
                {"d": get_settings().V2_TASK.terminal_retention_days},
            )
        ).all()

    processed = 0
    for task_id, owner_id in candidates:
        tid = str(task_id)
        async with owner_session(runtime, str(owner_id)) as db:
            ev = await db.execute(text("DELETE FROM task_events WHERE task_id = :t"), {"t": tid})
            # 轮行先于消息删：source_message_id 是 RESTRICT FK，轮不先删消息删不动
            rd = await db.execute(text("DELETE FROM task_rounds WHERE task_id = :t"), {"t": tid})
            msg = await db.execute(text("DELETE FROM task_messages WHERE task_id = :t"), {"t": tid})
            released = await _release_reservations(db, tid)
            if released:
                await _apply_release_accounting(db, tid, owner_id, released, refund_storage=True)
            dirty = bool(ev.rowcount or rd.rowcount or msg.rowcount or released)
        # post-commit 物理删（账面先行已提交；幂等 rmtree，失败留下一轮兜底）
        try:
            runtime.storage.delete_task_storage(tid)
        except Exception:
            logger.exception("terminal sweep physical delete failed: task_id=%s", tid)
        if dirty:
            processed += 1
    return processed


# ---------------------------------------------------------------------------
# dispatcher_loop：四作业串行常驻循环（outbox_loop 范式）
# ---------------------------------------------------------------------------


async def dispatcher_loop(runtime: V2Runtime, *, poll_seconds: float | None = None) -> None:
    """常驻调度循环（lifespan 后台协程）：领槽/回收/双清扫四作业串行，单轮异常只
    记录不外抛（DB 抖动不得杀死进程）；仅 CancelledError 穿透供 lifespan 关停取消。
    首轮立即执行，其后按 poll_seconds（缺省 Settings.V2_TASK.dispatcher_poll_seconds）
    轮询。"""
    if poll_seconds is None:
        poll_seconds = get_settings().V2_TASK.dispatcher_poll_seconds
    while True:
        try:
            claimed = await dispatch_once(runtime)
            reclaimed = await reclaim_once(runtime)
            upload_swept = await sweep_upload_ttl(runtime)
            terminal_cleaned = await sweep_terminal_cleanup(runtime)
            if claimed or reclaimed or upload_swept or terminal_cleaned:
                logger.info(
                    "task dispatcher cycle claimed=%d reclaimed=%d "
                    "upload_swept=%d terminal_cleaned=%d",
                    claimed,
                    reclaimed,
                    upload_swept,
                    terminal_cleaned,
                )
        except Exception:
            logger.exception("task dispatcher cycle failed; will retry")
        await asyncio.sleep(poll_seconds)
