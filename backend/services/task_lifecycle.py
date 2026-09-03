"""任务生命周期业务逻辑（手册 §7.2.1 锁语义表、§7.8、PRD §4.5.4）。

- complete：不先抢占 mutation lock。无锁检测活动轮（存在则 request_abort
  绕锁中止）→ 等待并获取 task mutation lock（与发送同一把 round lock）→
  持锁复验 running → 置 completed → 回收容器 + 失效令牌
- delete：持锁删除（先 round lock 再 data lock，与发送的加锁顺序一致防
  死锁）；先停容器再级联删除 DB 行与任务专属文件（上传目录、扩展文件）；
  项目工作区目录不动（PRD §4.5.6 删除约束）
- abort：仅 running 且存在活动轮可中止（PRD §4.5.6 中止约束）；中止不改
  任务终态（§7.8.1）
- 专家下架联动（§7.8）：该专家 running 任务原子置 completed + 回收容器

锁的获取顺序约定（全代码库一致）：round lock → data lock。
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.task import Task
from backend.services.task_locks import task_data_lock, task_round_lock
from backend.services.task_service import (
    TaskNotFoundError,
    TaskStateError,
    _get_owned_task,
)

LifecycleManager = object  # PiEngineManager（避免循环导入，鸭子类型）


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def complete_task(db: AsyncSession, user_id: int, task_id: int, manager) -> Task:
    """结束任务（PRD：仅 running 可结束；完成后不可继续发送）。"""
    task = await _get_owned_task(db, user_id, task_id)
    if task.status != "running":
        raise TaskStateError("仅进行中的任务可以结束")
    if manager.has_active_round(task_id):
        await manager.request_abort(task_id)  # 绕过 mutation lock（§7.2.1）
    async with task_round_lock(task_id):
        refreshed = await db.get(Task, task_id)
        if refreshed is None or refreshed.user_id != user_id:
            raise TaskNotFoundError("任务不存在")
        if refreshed.status != "running":
            raise TaskStateError("仅进行中的任务可以结束")
        refreshed.status = "completed"
        refreshed.updated_at = _now()
        await db.commit()
        await db.refresh(refreshed)
        await manager.stop_container(task_id)
    return refreshed


async def delete_task(
    db: AsyncSession,
    user_id: int,
    task_id: int,
    manager,
    *,
    task_files_root,
    extensions_root,
) -> None:
    """删除任务：级联会话/消息/文件元数据 + 任务上传目录 + 扩展文件。"""
    task = await _get_owned_task(db, user_id, task_id)
    async with task_round_lock(task_id):
        async with task_data_lock(task_id):
            await manager.stop_container(task_id)  # 先停容器再删数据
            await db.delete(task)
            await db.commit()
            # 任务专属文件（PRD §4.5.6：项目原有目录不受影响）
            shutil.rmtree(
                task_files_root / f"task-{task_id}", ignore_errors=True
            )
            (extensions_root / f"task-{task_id}.ts").unlink(missing_ok=True)


async def complete_expert_running_tasks(
    db: AsyncSession, expert_id: int, manager
) -> int:
    """专家下架联动（§7.8）：running 任务原子置 completed + 回收容器。"""
    rows = (
        await db.execute(
            select(Task).where(Task.expert_id == expert_id, Task.status == "running")
        )
    ).scalars()
    tasks = list(rows)
    for task in tasks:
        if manager.has_active_round(task.id):
            await manager.request_abort(task.id)
    if tasks:
        await db.execute(
            update(Task)
            .where(Task.expert_id == expert_id, Task.status == "running")
            .values(status="completed", updated_at=_now())
        )
        await db.commit()
    for task in tasks:
        await manager.stop_container(task.id)
    return len(tasks)
