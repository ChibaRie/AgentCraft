"""V2 任务域三本账对称释放原语（Phase 6 T3）。

契约出处：DB Design §4.2:152/§5:177、Phase 6 计划 T3 要点与 D16/D18
（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）。模块纪律：

- **按任务当前 status 分档释放**（计划 T3 要点对 DB §5:177 的收口）：
  completed/failed/aborted 释放 active、task_root 与字节账保留 7 天
  （sweep_terminal_cleanup 释放）；deleted 即时全清——存储账全退；
- 退还额以本次 ``UPDATE ... WHERE state='held' RETURNING bytes`` 汇总为闸门，
  零行即零退（幂等不二次退账）；条件减法带下限谓词（负值防御 + ERROR 日志）；
- 调用方会话必须已 set_current_owner；任务状态应已落位（本原语按落位后的
  status 选档）；任务行不存在（D18 物理删竞态）→ 静默返回（D16 弃权语义）；
- 经 ``task_service`` 再导出（冻结接口位置不变），executor/settle（T6a）、
  注销钩子（T6b）、清扫作业（T5）可直接消费。
"""

import logging

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.v2.models import PlatformSlot, Task, TaskReservation
from backend.v2.task_views import _parse_id

logger = logging.getLogger("agentcraft.task")

# 非 deleted 终态（active 释放档；task_root/字节账保留 7 天——DB §5:177）
_TERMINAL_NON_DELETED = frozenset({"completed", "failed", "aborted"})


async def _release_reservations(
    db: AsyncSession, task_id, *, kinds: tuple[str, ...] | None = None
) -> list:
    """held → released 条件翻转（RETURNING 闸门）：kinds=None 释放全部 kind。
    返回本次翻转的 (kind, bytes) 行——退账以此为唯一依据，零行即零退。"""
    stmt = update(TaskReservation).where(
        TaskReservation.task_id == task_id,
        TaskReservation.state == "held",
    )
    if kinds is not None:
        stmt = stmt.where(TaskReservation.kind.in_(kinds))
    rows = await db.execute(
        stmt.values(state="released")
        .returning(TaskReservation.kind, TaskReservation.bytes)
        .execution_options(synchronize_session=False)
    )
    return list(rows.all())


async def _refund_storage(db: AsyncSession, owner_id, nbytes: int) -> None:
    """存储账条件退还（用户 + 平台两本账）：下限谓词防负值；账面不足即 ERROR
    日志（账实不符，拒绝写负、不阻断释放），人工对账。"""
    user_paid = await db.execute(
        text(
            "UPDATE user_quota_usage SET retained_storage_bytes = retained_storage_bytes - :n "
            "WHERE user_id = :u AND retained_storage_bytes >= :n"
        ),
        {"n": nbytes, "u": owner_id},
    )
    if user_paid.rowcount == 0:
        logger.error(
            "任务释放退还存储：用户账面不足（拒绝写负，须人工对账）user_id=%s bytes=%d",
            owner_id,
            nbytes,
        )
    platform_paid = await db.execute(
        text(
            "UPDATE platform_storage SET retained_storage_bytes = retained_storage_bytes - :n "
            "WHERE singleton AND retained_storage_bytes >= :n"
        ),
        {"n": nbytes},
    )
    if platform_paid.rowcount == 0:
        logger.error("任务释放退还存储：平台账面不足（拒绝写负，须人工对账）bytes=%d", nbytes)


async def _apply_release_accounting(
    db: AsyncSession,
    task_id,
    owner_id,
    released: list,
    *,
    refund_storage: bool,
) -> None:
    """按 RETURNING 行对称回账：running→槽位 free + running_tasks-1；
    active→active_tasks-1；字节账退还仅在 deleted 档。"""
    running_n = sum(1 for kind, _ in released if kind == "running")
    active_n = sum(1 for kind, _ in released if kind == "active")
    if running_n:
        # 槽位释放（DB §4.2:152：置 free、清 task_id 与 leased_until；无租约行 0 行幂等静默）
        await db.execute(
            update(PlatformSlot)
            .where(PlatformSlot.task_id == task_id, PlatformSlot.state == "leased")
            .values(state="free", task_id=None, leased_until=None)
            .execution_options(synchronize_session=False)
        )
        # 下限谓词（T5 承接审查项，与 _refund_storage 同型）：账面不足拒绝写负 + ERROR
        decremented = await db.execute(
            text(
                "UPDATE user_quota_usage SET running_tasks = running_tasks - :n "
                "WHERE user_id = :u AND running_tasks >= :n"
            ),
            {"n": running_n, "u": owner_id},
        )
        if decremented.rowcount == 0:
            logger.error(
                "任务释放递减 running_tasks：账面不足（拒绝写负，须人工对账）user_id=%s n=%d",
                owner_id,
                running_n,
            )
    if active_n:
        decremented = await db.execute(
            text(
                "UPDATE user_quota_usage SET active_tasks = active_tasks - :n "
                "WHERE user_id = :u AND active_tasks >= :n"
            ),
            {"n": active_n, "u": owner_id},
        )
        if decremented.rowcount == 0:
            logger.error(
                "任务释放递减 active_tasks：账面不足（拒绝写负，须人工对账）user_id=%s n=%d",
                owner_id,
                active_n,
            )
    if refund_storage:
        nbytes = sum(int(n or 0) for _, n in released)
        if nbytes > 0:
            await _refund_storage(db, owner_id, nbytes)


async def release_task_holdings(db: AsyncSession, *, task_id: str, owner_id: str) -> None:
    """三本账对称释放原语（调用方会话已 set_current_owner；任务状态落位后调用）。

    按任务当前 status 分档（计划 T3 要点对 DB §5:177 的收口）：

    - 轮账（任何状态）：kind='running' held→released；有释放则 platform_slots 置
      free + running_tasks 递减；
    - 终态 completed/failed/aborted：另 kind='active' released + active_tasks 递减；
      task_root/产物副本与字节账保留 7 天（DB §5:177），归 sweep_terminal_cleanup
      释放；
    - deleted：全部 held released（含 task_root）+ 存储账全退——退还额以本次
      ``UPDATE ... WHERE state='held' RETURNING bytes`` 汇总为闸门，零行即零退；
      条件减法带下限 ``WHERE retained_storage_bytes >= :n``（负值防御 + ERROR 日志）。

    任务行不存在（D18 物理删竞态）→ 静默返回（D16 弃权语义）。settle→ready 等
    非终态调用只回收轮账，active/task_root 不受影响。
    """
    tid = _parse_id(task_id, "task_id")
    owner_uuid = _parse_id(owner_id, "owner_id")
    status = (await db.execute(select(Task.status).where(Task.id == tid))).scalar_one_or_none()
    if status is None:
        return
    if status == "deleted":
        released = await _release_reservations(db, tid)
        await _apply_release_accounting(db, tid, owner_uuid, released, refund_storage=True)
        return
    released = await _release_reservations(db, tid, kinds=("running",))
    if status in _TERMINAL_NON_DELETED:
        released += await _release_reservations(db, tid, kinds=("active",))
    await _apply_release_accounting(db, tid, owner_uuid, released, refund_storage=False)
