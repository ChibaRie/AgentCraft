"""V2 任务域读面（Phase 6 T3）：D14 视图装配与任务/配额只读服务。

契约出处：Sup §1.2/§4/§7、Phase 6 计划裁决 D14
（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）。本模块为任务域
的**读平面**（视图装配 + get/list/quota 只读服务）与共享边界助手；写平面
（创建/commit/终态/释放）在 ``task_service.py`` 并从本模块导入共享助手。
纪律：

- 视图字段钉死 D14：排除 lease_owner/lease_epoch（红线）；round 摘要仅
  id/state/attempt；``_strip_lease_fields`` 构造器为回调响应面（T7
  query_task_state）提供同红线剥除式防线；Phase 8 T2（Sup §10.3）键集增
  expert/provider 两枚展示字段——**不加 skills 键**（skills 经 discover 详情
  二次拉取，不进任务视图）；
- deleted 任务统一 404（Sup §7「越权、不存在、已删除资源一律 404」）；
- 服务函数不 begin 不 commit；调用方会话必须已 set_current_owner（RLS 生效
  前提），本模块不重复设置；
- task_id 等路径段参数一律严格 UUID 解析（非法 → 400）——TaskStorage 路径段
  永不接收外部裸串（上游审查顺延约束）。

get_task_view/list_tasks/get_quota_view 经 ``task_service`` 再导出（冻结接口
位置不变），也可直接从本模块消费；``get_task_quota_view``（D7g 任务粒度
配额视图，Phase 6 T8a 补）直接从本模块消费。
"""

import uuid as _uuid
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.author_service import _reject_invalid_uuid as _parse_uuid
from backend.v2.models import (
    Expert,
    ExpertRevision,
    Task,
    TaskEvent,
    TaskFile,
    TaskMessage,
    TaskRound,
    UserProvider,
    UserQuota,
    UserQuotaUsage,
)

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
    """D14 任务视图批量装配（rounds/counts/expert/provider 各一条集合查询，避免
    分页 N+1）。

    counts 排除 state='deleted' 墓碑行；active_round 取活跃态（部分唯一索引保证
    至多一条）；initial_round 取 source_message_id = initial_message_id 的轮
    （round_source_message 唯一）。expert/provider 展示字段为 Sup §10.3（Phase 8
    T2）增量，null 语义见 ``_expert_brief_by_revision``。
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
                Task.expert_revision_id,
                Task.provider_id,
                Task.provider_model_id,
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

    experts_by_rev = await _expert_brief_by_revision(db, [t.expert_revision_id for t in tasks])
    provider_ids = {t.provider_id for t in tasks}
    base_by_provider: dict[_uuid.UUID, str] = {}
    if provider_ids:
        provider_rows = (
            await db.execute(
                select(UserProvider.id, UserProvider.base_url).where(
                    UserProvider.id.in_(provider_ids)
                )
            )
        ).all()
        base_by_provider = {row.id: row.base_url for row in provider_rows}

    def _host_of(url: str | None) -> str | None:
        if not url:
            return None
        try:
            return urlparse(url).hostname
        except ValueError:
            return None

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
        host = _host_of(base_by_provider.get(t.provider_id))
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
            # Sup §10.3 展示字段（Phase 8 T2；2026-09-17 去目录化）：expert 为
            # published_read 可见时的 {name, avatar_url}；provider.display_name =
            # 上游 host（base_url 解析），行缺失/解析失败 → 整 provider=null。
            "expert": experts_by_rev.get(t.expert_revision_id),
            "provider": (
                {"display_name": host, "model": t.provider_model_id}
                if host is not None
                else None
            ),
        }
    return [views_by_id[tid] for tid in task_ids if tid in views_by_id]


async def _expert_brief_by_revision(
    db: AsyncSession, revision_ids: list[_uuid.UUID | None]
) -> dict[_uuid.UUID, dict]:
    """Sup §10.3（Phase 8 T2）：expert 展示字段批量装配（一条集合查询防 N+1）。

    可见性钉「实体当前 published 指针」（契约原文：join 走 published_read、仅认
    实体当前 published 指针——历史任务不回溯旧版内容）：join experts ON
    published_revision_id 指针相等且 status='published'。status 条件不可省——
    experts RLS 对 owner 本人放行 draft 行（0001:1074-1077 的 OR owner 分支），
    缺它则 takedown（status→draft、指针不清，§10.6）后作者本人的任务视图仍能
    读出旧内容，违背「takedown 后均为 null」。不可见面（takedown/作者再发布
    新版/revision 行 RLS 不可见）→ 该 revision 无条目 → 视图侧取 null 不抛。
    """
    ids = {rid for rid in revision_ids if rid is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(ExpertRevision.id, ExpertRevision.content_json)
            .join(
                Expert,
                and_(
                    Expert.published_revision_id == ExpertRevision.id,
                    Expert.status == "published",
                ),
            )
            .where(ExpertRevision.id.in_(ids))
        )
    ).all()
    return {
        row.id: {
            "name": (row.content_json or {}).get("name"),
            "avatar_url": (row.content_json or {}).get("avatar_url"),
        }
        for row in rows
    }


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


async def get_task_quota_view(db: AsyncSession, *, owner_id: str, task_id: str) -> dict:
    """任务粒度用量视图（D7g / Sup §4:105：当前用量/上限/输入冻结状态）。

    用量按任务文件账（两方向存活行，墓碑不计）聚合；上限为任务域三把门
    （PRD §4.1:108-109 契约钉值，从 task_file_service 单一事实源函数域导入——
    本模块先于 task_file_service 被其导入，模块级反向导入成环）；input_frozen
    即 input_committed_at 已设（冻结后任何文件变更 409 INPUT_COMMITTED）。
    权威判定仍在写事务内（本视图只读）。任务缺失/他人（RLS 0 行）/已删除
    → 统一 404（Sup §7）。
    """
    tid = _parse_id(task_id, "task_id")
    _parse_id(owner_id, "owner_id")
    task = (
        await db.execute(
            select(Task.input_committed_at).where(Task.id == tid, Task.status != "deleted")
        )
    ).first()
    if task is None:
        raise _task_not_found()
    rows = (
        await db.execute(
            select(
                TaskFile.direction,
                func.count(),
                func.coalesce(func.sum(TaskFile.size_bytes), 0),
            )
            .where(TaskFile.task_id == tid, TaskFile.state != "deleted")
            .group_by(TaskFile.direction)
        )
    ).all()
    by_direction = {d: (int(n), int(s)) for d, n, s in rows}
    inputs_count, inputs_bytes = by_direction.get("input", (0, 0))
    outputs_count, outputs_bytes = by_direction.get("output", (0, 0))
    from backend.v2.task_file_service import (  # 函数域导入防环（见 docstring）
        _MAX_FILES_PER_TASK,
        _MAX_SINGLE_FILE_BYTES,
        _MAX_TASK_INPUT_BYTES,
    )

    return {
        "usage": {
            "inputs_count": inputs_count,
            "outputs_count": outputs_count,
            "inputs_bytes": inputs_bytes,
            "outputs_bytes": outputs_bytes,
            "total_bytes": inputs_bytes + outputs_bytes,
        },
        "limits": {
            "max_files_per_task": _MAX_FILES_PER_TASK,
            "max_single_file_bytes": _MAX_SINGLE_FILE_BYTES,
            "max_task_bytes": _MAX_TASK_INPUT_BYTES,
        },
        "input_frozen": task.input_committed_at is not None,
    }


# ---------------------------------------------------------------------------
# 补拉读面（Phase 6 T8b：messages 全量正文 / events 事实序列 + 快照）
# ---------------------------------------------------------------------------


async def _require_live_task(db: AsyncSession, task_id: str) -> _uuid.UUID:
    """任务存在性门（读面统一 404）：缺失/他人（RLS 0 行）/已删除同形（Sup §7）；
    返回规范化 UUID（非法路径段 400）——SSE 流建立前的 JSON 短路也走本门。"""
    tid = _parse_id(task_id, "task_id")
    exists = (
        await db.execute(select(Task.id).where(Task.id == tid, Task.status != "deleted"))
    ).scalar_one_or_none()
    if exists is None:
        raise _task_not_found()
    return tid


async def list_task_messages(
    db: AsyncSession, *, owner_id: str, task_id: str, after: int, limit: int
) -> list[dict]:
    """消息补拉（Sup §1.2:26）：event_sequence 升序、全量正文，``after`` 游标 +
    ``limit`` 截断（1≤limit≤200 由路由层 Query 门校验）。不可用于状态恢复——
    事实面是 /events。"""
    tid = await _require_live_task(db, task_id)
    _parse_id(owner_id, "owner_id")
    rows = (
        (
            await db.execute(
                select(TaskMessage)
                .where(TaskMessage.task_id == tid, TaskMessage.event_sequence > after)
                .order_by(TaskMessage.event_sequence)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(m.id),
            "event_sequence": int(m.event_sequence),
            "author": m.author,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in rows
    ]


async def list_task_events(
    db: AsyncSession, *, owner_id: str, task_id: str, after: int, limit: int | None
) -> dict:
    """事件补拉（Sup §1.2:27）：sequence 升序 ``(sequence, type, payload)`` 行 +
    当前任务快照 {status, event_sequence}（同一会话读出；after > watermark 时
    事件自然空集、快照仍在）。``limit=None`` 不截断（SSE 重放面全量拉取）。"""
    tid = await _require_live_task(db, task_id)
    _parse_id(owner_id, "owner_id")
    snap = (await db.execute(select(Task.status, Task.event_sequence).where(Task.id == tid))).one()
    query = (
        select(TaskEvent.sequence, TaskEvent.type, TaskEvent.payload_json)
        .where(TaskEvent.task_id == tid, TaskEvent.sequence > after)
        .order_by(TaskEvent.sequence)
    )
    if limit is not None:
        query = query.limit(limit)
    rows = (await db.execute(query)).all()
    return {
        "snapshot": {"status": snap.status, "event_sequence": int(snap.event_sequence)},
        "events": [(int(r.sequence), r.type, r.payload_json) for r in rows],
    }
