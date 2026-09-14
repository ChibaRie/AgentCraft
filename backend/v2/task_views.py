"""V2 任务域读面（Phase 6 T3）：D14 视图装配与任务/配额只读服务。

契约出处：Sup §1.2/§4/§7、Phase 6 计划裁决 D14
（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）。本模块为任务域
的**读平面**（视图装配 + get/list/quota 只读服务）与共享边界助手；写平面
（创建/commit/终态/释放）在 ``task_service.py`` 并从本模块导入共享助手。
纪律：

- 视图字段钉死 D14：排除 lease_owner/lease_epoch（红线）；round 摘要仅
  id/state/attempt；``_strip_lease_fields`` 构造器为回调响应面（T7
  query_task_state）提供同红线剥除式防线；
- deleted 任务统一 404（Sup §7「越权、不存在、已删除资源一律 404」）；
- 服务函数不 begin 不 commit；调用方会话必须已 set_current_owner（RLS 生效
  前提），本模块不重复设置；
- task_id 等路径段参数一律严格 UUID 解析（非法 → 400）——TaskStorage 路径段
  永不接收外部裸串（上游审查顺延约束）。

get_task_view/list_tasks/get_quota_view 经 ``task_service`` 再导出（冻结接口
位置不变），也可直接从本模块消费。
"""

import uuid as _uuid

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.author_service import _reject_invalid_uuid as _parse_uuid
from backend.v2.models import Task, TaskFile, TaskRound, UserQuota, UserQuotaUsage

# 活跃轮状态集（one_active_round_per_task 部分唯一索引同词表）
_ACTIVE_ROUND_STATES = ("pending", "running", "cancelling")
_LIST_PAGE_MAX = 100


def _validation_error(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": message})


def _parse_id(value, field: str) -> _uuid.UUID:
    """边界 UUID 解析（author_service._reject_invalid_uuid 的 str 强转包装）：
    非法串/路径段形态 → 400；顺带容忍调用方传入 asyncpg UUID 对象。"""
    return _parse_uuid(str(value), field)


def _task_not_found() -> AgentCraftError:
    """统一 404：缺失/他人（RLS 0 行）/已删除同形（Sup §7）。"""
    return AgentCraftError(ErrorCode.TASK_NOT_FOUND, "任务不存在", http_status=404)


def _round_out(row) -> dict:
    """round 摘要（D14：仅 id/state/attempt——lease_owner/lease_epoch 红线排除）。"""
    return {"id": str(row.id), "state": row.state, "attempt": int(row.attempt)}


def _strip_lease_fields(node, excluded: frozenset[str] = frozenset()):
    """lease_* 字段剥除构造器（Phase 6 T7）：递归剥除键名以 ``lease_`` 开头的
    字段，外加 ``excluded`` 显式声明集（query_task_state 描述符 permissions.
    exclude 的服务端强制值源）。D14 红线的构造器级防线——视图装配已排除，本
    构造器对任意输入视图再收口一次；回调响应面消费，剥除式而非 400 拒绝式。"""
    if isinstance(node, dict):
        return {
            key: _strip_lease_fields(value, excluded)
            for key, value in node.items()
            if not str(key).startswith("lease_") and key not in excluded
        }
    if isinstance(node, list):
        return [_strip_lease_fields(item, excluded) for item in node]
    return node


async def _load_task_views(db: AsyncSession, task_ids: list[_uuid.UUID]) -> list[dict]:
    """D14 任务视图批量装配（rounds/counts 各一条集合查询，避免分页 N+1）。

    counts 排除 state='deleted' 墓碑行；active_round 取活跃态（部分唯一索引保证
    至多一条）；initial_round 取 source_message_id = initial_message_id 的轮
    （round_source_message 唯一）。
    """
    if not task_ids:
        return []
    tasks = (
        await db.execute(
            select(
                Task.id,
                Task.status,
                Task.abort_reason,
                Task.created_at,
                Task.input_committed_at,
                Task.input_manifest_sha256,
                Task.event_sequence,
                Task.initial_message_id,
            ).where(Task.id.in_(task_ids))
        )
    ).all()
    rounds = (
        await db.execute(
            select(
                TaskRound.task_id,
                TaskRound.id,
                TaskRound.state,
                TaskRound.attempt,
                TaskRound.source_message_id,
            ).where(TaskRound.task_id.in_(task_ids))
        )
    ).all()
    count_rows = (
        await db.execute(
            select(TaskFile.task_id, TaskFile.direction, func.count())
            .where(TaskFile.task_id.in_(task_ids), TaskFile.state != "deleted")
            .group_by(TaskFile.task_id, TaskFile.direction)
        )
    ).all()

    counts: dict[_uuid.UUID, dict[str, int]] = {}
    for tid, direction, n in count_rows:
        counts.setdefault(tid, {})[direction] = int(n)
    rounds_by_task: dict[_uuid.UUID, list] = {}
    for r in rounds:
        rounds_by_task.setdefault(r.task_id, []).append(r)

    views_by_id: dict[_uuid.UUID, dict] = {}
    for t in tasks:
        task_rounds = rounds_by_task.get(t.id, [])
        active = next((r for r in task_rounds if r.state in _ACTIVE_ROUND_STATES), None)
        initial = (
            next(
                (r for r in task_rounds if r.source_message_id == t.initial_message_id),
                None,
            )
            if t.initial_message_id is not None
            else None
        )
        views_by_id[t.id] = {
            "id": str(t.id),
            "status": t.status,
            "abort_reason": t.abort_reason,
            "created_at": t.created_at.isoformat(),
            "input_committed": t.input_committed_at is not None,
            "input_manifest_sha256": t.input_manifest_sha256,
            "event_sequence": int(t.event_sequence),
            "active_round": _round_out(active) if active else None,
            "initial_round": _round_out(initial) if initial else None,
            "counts": {
                "inputs": counts.get(t.id, {}).get("input", 0),
                "outputs": counts.get(t.id, {}).get("output", 0),
            },
        }
    return [views_by_id[tid] for tid in task_ids if tid in views_by_id]


async def _load_task_view(db: AsyncSession, task_id: _uuid.UUID) -> dict:
    return (await _load_task_views(db, [task_id]))[0]


async def get_task_view(db: AsyncSession, *, owner_id: str, task_id: str) -> dict:
    """D14 任务快照（无锁读）：缺失/他人（RLS 0 行）/已删除 → 统一 404。"""
    tid = _parse_id(task_id, "task_id")
    exists = (
        await db.execute(select(Task.id).where(Task.id == tid, Task.status != "deleted"))
    ).scalar_one_or_none()
    if exists is None:
        raise _task_not_found()
    return await _load_task_view(db, tid)


async def list_tasks(db: AsyncSession, *, owner_id: str, page: int, size: int) -> dict:
    """owner 任务列表（分页，created_at DESC + id DESC 稳定序；deleted 不可见）。

    返回 ``{"items", "total", "page", "size"}``（Sup §7 列表信封）。
    """
    if page < 1 or size < 1 or size > _LIST_PAGE_MAX:
        raise _validation_error("分页参数非法（page≥1，1≤size≤100）")
    total = int(
        (
            await db.execute(select(func.count()).select_from(Task).where(Task.status != "deleted"))
        ).scalar_one()
    )
    ids = (
        (
            await db.execute(
                select(Task.id)
                .where(Task.status != "deleted")
                .order_by(Task.created_at.desc(), Task.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    items = await _load_task_views(db, list(ids))
    return {"items": items, "total": total, "page": page, "size": size}


def _quota_model_defaults() -> dict[str, int]:
    """读 models/catalog.py::UserQuota 列默认值（单一事实源，不硬编码副本）。"""
    cols = UserQuota.__table__.columns
    return {
        name: int(cols[name].default.arg)
        for name in (
            "max_daily_tasks",
            "max_active_tasks",
            "max_running_tasks",
            "max_retained_storage_bytes",
        )
    }


async def get_quota_view(db: AsyncSession, *, owner_id: str) -> dict:
    """owner 四维用量与上限（只读；权威判定仍在写事务内）。usage_daily 按 DB
    CURRENT_DATE 对齐（与创建 upsert 同一时钟源）。"""
    uid = _parse_id(owner_id, "owner_id")
    limits = (
        await db.execute(
            select(
                UserQuota.max_daily_tasks,
                UserQuota.max_active_tasks,
                UserQuota.max_running_tasks,
                UserQuota.max_retained_storage_bytes,
            ).where(UserQuota.user_id == uid)
        )
    ).first()
    usage = (
        await db.execute(
            select(
                UserQuotaUsage.active_tasks,
                UserQuotaUsage.running_tasks,
                UserQuotaUsage.retained_storage_bytes,
            ).where(UserQuotaUsage.user_id == uid)
        )
    ).first()
    started_today = (
        await db.execute(
            text("SELECT tasks_started FROM usage_daily WHERE user_id = :u AND day = CURRENT_DATE"),
            {"u": uid},
        )
    ).scalar_one_or_none()
    defaults = _quota_model_defaults()
    _lim = (
        {
            "max_daily_tasks": int(limits.max_daily_tasks),
            "max_active_tasks": int(limits.max_active_tasks),
            "max_running_tasks": int(limits.max_running_tasks),
            "max_retained_storage_bytes": int(limits.max_retained_storage_bytes),
        }
        if limits
        else defaults
    )
    return {
        "usage": {
            "tasks_started_today": int(started_today or 0),
            "active_tasks": int(usage.active_tasks) if usage else 0,
            "running_tasks": int(usage.running_tasks) if usage else 0,
            "retained_storage_bytes": int(usage.retained_storage_bytes) if usage else 0,
        },
        "limits": _lim,
    }
