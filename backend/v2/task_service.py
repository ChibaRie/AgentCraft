"""V2 任务域核心服务（Phase 6 T3）：创建/输入冻结/终态意图/三本账释放。

契约出处：Sup §1.2/§1.4/§4、DB Design §4.2/§5、Phase 6 计划裁决 D4/D7/D14/D19
（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）。模块纪律：

- **服务函数不 begin 不 commit**：事务由调用方收口（路由 owner_session / 测试
  owner_tx）；调用方会话必须已 set_current_owner（GUC 事务本地，RLS 生效前提），
  本模块不重复设置；
- 条件 UPDATE rowcount 仲裁全覆盖（D4：终结类翻转不裸写）；事件序号原子分配
  （``UPDATE tasks ... RETURNING``，禁 ``max(sequence)+1``，Sup §1.4:56）；
- 终态账务分档（计划 T3 要点对 DB §5:177 的收口）：completed/failed/aborted 释放
  active、task_root 与字节账保留 7 天（sweep_terminal_cleanup 释放）；deleted 即时
  全清——存储账全退，物理删 task-storage 由调用方 post-commit 兜底（账面先行，
  本模块不做任何磁盘 I/O）；
- 提示词预算对读 ``Settings.SKILL_PROMPT_MAX_BYTES``（D7d，65,536），不硬编码
  字面量；超限 400 VALIDATION_ERROR（HTTPException 形态，不新增错误码）；
- 读平面（D14 视图/get/list/quota）与共享边界助手在 ``task_views.py``，三本账
  释放原语在 ``task_release.py``——本模块再导出其公开服务（冻结接口位置不变）。
"""

import hashlib
import logging
import uuid as _uuid
from datetime import datetime, timezone

from sqlalchemy import event, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.errors import AgentCraftError, ErrorCode
from backend.v2.content_hash import canonical_json
from backend.v2.ids import uuid7
from backend.v2.models import (
    ExpertRevision,
    RevisionTool,
    Task,
    TaskEvent,
    TaskFile,
    TaskMessage,
    TaskReservation,
    TaskRound,
    UserQuota,
    UserQuotaUsage,
)
from backend.v2.task_release import release_task_holdings
from backend.v2.task_state import assert_transition
from backend.v2.task_streams import TaskStreamRegistry, queued_frame, status_frame
from backend.v2.task_views import (
    _ACTIVE_ROUND_STATES,
    _load_task_view,
    _parse_id,
    _task_not_found,
    _validation_error,
    get_quota_view,
    get_task_view,
    list_tasks,
)
from backend.v2.tool_service import assert_tool_enabled

__all__ = [
    "abort_task",
    "complete_task",
    "commit_input",
    "create_task",
    "delete_task",
    "get_quota_view",
    "get_task_view",
    "list_tasks",
    "release_task_holdings",
    "send_message",
]

logger = logging.getLogger("agentcraft.task")


def _transition_conflict(message: str = "任务状态已变化") -> AgentCraftError:
    return AgentCraftError(ErrorCode.TASK_INVALID_TRANSITION, message, http_status=409)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status_payload(status: str, reason: str | None) -> dict:
    payload = {"status": status}
    if reason is not None:
        payload["reason"] = reason
    return payload


# ---------------------------------------------------------------------------
# 事件序号与事件行
# ---------------------------------------------------------------------------


async def _allocate_event_sequence(db: AsyncSession, task_id: _uuid.UUID, count: int = 1) -> int:
    """事件序号原子分配（Sup §1.4:56）：``UPDATE tasks SET event_sequence =
    event_sequence + :count RETURNING``，返回本批首个序号（同事务后续事件依次 +1）。"""
    row = await db.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(event_sequence=Task.event_sequence + count)
        .returning(Task.event_sequence)
        .execution_options(synchronize_session=False)
    )
    return int(row.scalar_one()) - count + 1


def _add_event(
    db: AsyncSession,
    *,
    task_id: _uuid.UUID,
    owner_id: _uuid.UUID,
    sequence: int,
    event_type: str,
    payload: dict,
    message_id: _uuid.UUID | None = None,
    round_id: _uuid.UUID | None = None,
) -> None:
    db.add(
        TaskEvent(
            task_id=task_id,
            owner_id=owner_id,
            sequence=sequence,
            type=event_type,
            payload_json=payload,
            message_id=message_id,
            round_id=round_id,
        )
    )


# ---------------------------------------------------------------------------
# 配额（四维：daily/active 于创建时推进；running 于领轮；storage 于 commit）
# ---------------------------------------------------------------------------


async def _ensure_quota_rows(db: AsyncSession, owner_id: _uuid.UUID) -> None:
    """配额行惰性物化（注册路径未建行时防误判 429）：ORM 列默认值即平台默认
    （5/3/1/1GiB，models/catalog.py::UserQuota 单一事实源）。"""
    await db.execute(
        pg_insert(UserQuota)
        .values(user_id=owner_id)
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    await db.execute(
        pg_insert(UserQuotaUsage)
        .values(user_id=owner_id)
        .on_conflict_do_nothing(index_elements=["user_id"])
    )


async def _charge_creation_quota(db: AsyncSession, owner_id: _uuid.UUID) -> None:
    """创建时点配额推进（DB §4.2:2-3）：锁 user_quota_usage FOR UPDATE → 当日
    usage_daily 门（当日 upsert、跨日翻新）→ active_tasks 条件增。
    任一 rowcount=0 → 429（事务由调用方回滚，无半账）。"""
    await _ensure_quota_rows(db, owner_id)
    # 账目行行锁：与 commit 存储增 / 释放递减互斥（advisory 锁之外的第二道闸）
    await db.execute(
        select(UserQuotaUsage.user_id).where(UserQuotaUsage.user_id == owner_id).with_for_update()
    )
    daily = await db.execute(
        text(
            "INSERT INTO usage_daily (id, user_id, day, tasks_started) "
            "SELECT :i, :u, CURRENT_DATE, 1 "
            "WHERE (SELECT max_daily_tasks FROM user_quotas WHERE user_id = :u) > 0 "
            "ON CONFLICT (user_id, day) DO UPDATE SET "
            "tasks_started = usage_daily.tasks_started + 1 "
            "WHERE usage_daily.tasks_started < "
            "(SELECT max_daily_tasks FROM user_quotas WHERE user_id = :u)"
        ),
        {"i": uuid7(), "u": owner_id},
    )
    if daily.rowcount == 0:
        raise AgentCraftError(
            ErrorCode.QUOTA_DAILY_EXCEEDED, "今日任务创建数已达上限", http_status=429
        )
    active = await db.execute(
        text(
            "UPDATE user_quota_usage SET active_tasks = active_tasks + 1 "
            "WHERE user_id = :u AND active_tasks < "
            "(SELECT max_active_tasks FROM user_quotas WHERE user_id = :u)"
        ),
        {"u": owner_id},
    )
    if active.rowcount == 0:
        raise AgentCraftError(
            ErrorCode.QUOTA_ACTIVE_EXCEEDED, "活跃任务数已达上限", http_status=429
        )


# ---------------------------------------------------------------------------
# 输入校验助手
# ---------------------------------------------------------------------------


def _validate_initial_message(initial_message: str) -> None:
    """D7d：非空白 + 提示词预算（对读 Settings.SKILL_PROMPT_MAX_BYTES），先于一切写。"""
    if not isinstance(initial_message, str) or not initial_message.strip():
        raise _validation_error("initial_message 不能为空白")
    limit = get_settings().SKILL_PROMPT_MAX_BYTES
    if len(initial_message.encode("utf-8")) > limit:
        raise _validation_error(f"initial_message 超出提示词预算（{limit} 字节）")


def _require_provider_snapshot(snapshot: dict) -> tuple[_uuid.UUID, str, int]:
    """D14 快照键 {provider_id, provider_catalog_id, provider_model_id,
    provider_key_version}（与 ResolvedProvider 一一对应）；缺漏/非法 → 400。"""
    try:
        catalog_id = _uuid.UUID(str(snapshot["provider_catalog_id"]))
        model_id = str(snapshot["provider_model_id"])
        key_version = int(snapshot["provider_key_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _validation_error(
            "provider_snapshot 必须含 provider_catalog_id/provider_model_id/provider_key_version"
        ) from exc
    return catalog_id, model_id, key_version


async def _assert_revision_tools_enabled(db: AsyncSession, revision_id: _uuid.UUID) -> None:
    """第一时点工具校验（D4）：revision_tools 全集逐个断言启用（409 载体；
    0007 冻结触发器保证任务期工具集稳定）。"""
    rows = (
        await db.execute(
            select(RevisionTool.tool_id, RevisionTool.version).where(
                RevisionTool.expert_revision_id == revision_id
            )
        )
    ).all()
    for tool_id, version in rows:
        await assert_tool_enabled(db, tool_id, version, http_status=409)


async def _lock_task(db: AsyncSession, task_id: str) -> Task:
    """FOR UPDATE 锁定任务行（状态仲裁前提）。非法 UUID → 400；缺失/已删除 →
    统一 404（deleted 即不可见）。"""
    tid = _parse_id(task_id, "task_id")
    row = (
        await db.execute(select(Task).where(Task.id == tid).with_for_update())
    ).scalar_one_or_none()
    if row is None or row.status == "deleted":
        raise _task_not_found()
    return row


# ---------------------------------------------------------------------------
# 创建与输入冻结
# ---------------------------------------------------------------------------


async def create_task(
    db: AsyncSession,
    *,
    owner_id: str,
    expert_revision_id: str,
    provider_id: str,
    initial_message: str,
    provider_snapshot: dict,
) -> dict:
    """创建任务（DB §4.2 创建事务，D7c/D14 形状）。

    门序：owner 级 ``pg_advisory_xact_lock``（配额临界区串行化）→ initial_message
    校验（非空白 + 提示词预算，先于一切写）→ revision published 门（DB §4.1:4：
    非 published 不可被新任务选择）→ 配额推进（QUOTA_DAILY/QUOTA_ACTIVE，429）→
    第一时点工具校验（TOOL_REVOKED 409，D4）→ INSERT task(uploading, 快照列,
    event_sequence=0) → INSERT task_message(author=user) + message_saved 事件
    （message/event 同一 event sequence，DB §4.2:4）→ 回填 initial_message_id
    （use_alter 循环 FK）→ active reservation(kind='active', bytes=0, held)。

    返回 ``{"task": D14 视图, "message": {"id", "event_sequence"}}``（路由层包
    201 ``{data: {...}}`` 信封）。
    """
    revision_uuid = _parse_id(expert_revision_id, "expert_revision_id")
    provider_uuid = _parse_id(provider_id, "provider_id")
    owner_uuid = _parse_id(owner_id, "owner_id")
    catalog_id, model_id, key_version = _require_provider_snapshot(provider_snapshot)

    # DB §4.2:1：owner 级咨询锁（锁随事务收口自动释放，与 rate_limit 同型）
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"task-create:{owner_uuid}"},
    )
    _validate_initial_message(initial_message)
    published = (
        await db.execute(
            select(ExpertRevision.id).where(
                ExpertRevision.id == revision_uuid, ExpertRevision.status == "published"
            )
        )
    ).scalar_one_or_none()
    if published is None:
        raise AgentCraftError(
            ErrorCode.REVISION_NOT_PUBLISHED, "专家修订版本不存在或未发布", http_status=400
        )
    await _charge_creation_quota(db, owner_uuid)
    await _assert_revision_tools_enabled(db, revision_uuid)

    task = Task(
        id=uuid7(),
        owner_id=owner_uuid,
        expert_revision_id=revision_uuid,
        provider_id=provider_uuid,
        provider_catalog_id=catalog_id,
        provider_model_id=model_id,
        provider_key_version=key_version,
        status="uploading",
        event_sequence=0,
    )
    db.add(task)
    await db.flush()

    mid = uuid7()
    seq = await _allocate_event_sequence(db, task.id)  # → 1
    db.add(
        TaskMessage(
            id=mid,
            task_id=task.id,
            owner_id=owner_uuid,
            event_sequence=seq,
            author="user",
            content=initial_message,
        )
    )
    _add_event(
        db,
        task_id=task.id,
        owner_id=owner_uuid,
        sequence=seq,
        event_type="message_saved",
        payload={"message_id": str(mid), "event_sequence": seq, "author": "user"},
        message_id=mid,
    )
    # 消息行先落库：循环 FK（use_alter）未配 ORM relationship，flush 依赖序不保证
    await db.flush()
    task.initial_message_id = mid  # 循环 FK 回填（use_alter，0001:932-963）
    db.add(
        TaskReservation(
            id=uuid7(),
            task_id=task.id,
            user_id=owner_uuid,
            kind="active",
            bytes=0,
            state="held",
        )
    )
    await db.flush()
    view = await _load_task_view(db, task.id)
    return {"task": view, "message": {"id": str(mid), "event_sequence": seq}}


async def commit_input(
    db: AsyncSession, *, owner_id: str, task_id: str, manifest: list[dict]
) -> dict:
    """输入冻结（DB §4.2:5 + Sup §4）：锁 task FOR UPDATE → 必须 uploading（冻结后
    INPUT_COMMITTED 409；终态等非法态 TASK_INVALID_TRANSITION 409）→ manifest
    canonical SHA-256（复用 content_hash 规范化形态）→ staged→committed 一次性翻转
    → task_root reservation（bytes=Σinputs）→ 用户/平台存储条件增（rowcount=0 →
    QUOTA_STORAGE_EXCEEDED 429）→ 初始轮 pending → assert_transition(uploading→queued)
    → status_changed + round_queued 事件。

    返回 ``{"task", "manifest_sha256", "round_id", "event_sequence"}``（D14 commit 形状）。
    """
    task = await _lock_task(db, task_id)
    owner_uuid = _parse_id(owner_id, "owner_id")
    if task.status != "uploading":
        if task.input_committed_at is not None or task.status in ("queued", "running", "ready"):
            raise AgentCraftError(ErrorCode.INPUT_COMMITTED, "任务输入已冻结", http_status=409)
        assert_transition(task.status, "queued")  # 终态等非法态 → TASK_INVALID_TRANSITION 409

    manifest_sha256 = hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()
    task.input_manifest_sha256 = manifest_sha256
    task.input_committed_at = _now()
    # 契约时序：staged → committed（一次性；T4 上传停留 staged，deleted 墓碑不翻）
    await db.execute(
        update(TaskFile)
        .where(TaskFile.task_id == task.id, TaskFile.state == "staged")
        .values(state="committed")
        .execution_options(synchronize_session=False)
    )
    total_bytes = int(
        (
            await db.execute(
                select(func.coalesce(func.sum(TaskFile.size_bytes), 0)).where(
                    TaskFile.task_id == task.id,
                    TaskFile.direction == "input",
                    TaskFile.state == "committed",
                )
            )
        ).scalar_one()
    )
    db.add(
        TaskReservation(
            id=uuid7(),
            task_id=task.id,
            user_id=owner_uuid,
            kind="task_root",
            bytes=total_bytes,
            state="held",
        )
    )
    # 存储条件增（用户维 → 平台维；同一事务任一失败整体回滚，不留半账）
    user_inc = await db.execute(
        text(
            "UPDATE user_quota_usage SET retained_storage_bytes = retained_storage_bytes + :n "
            "WHERE user_id = :u AND retained_storage_bytes + :n <= "
            "(SELECT max_retained_storage_bytes FROM user_quotas WHERE user_id = :u)"
        ),
        {"n": total_bytes, "u": owner_uuid},
    )
    if user_inc.rowcount == 0:
        raise AgentCraftError(
            ErrorCode.QUOTA_STORAGE_EXCEEDED, "用户保留存储配额不足", http_status=429
        )
    platform_inc = await db.execute(
        text(
            "UPDATE platform_storage SET retained_storage_bytes = retained_storage_bytes + :n "
            "WHERE singleton AND retained_storage_bytes + :n <= max_retained_storage_bytes"
        ),
        {"n": total_bytes},
    )
    if platform_inc.rowcount == 0:
        raise AgentCraftError(
            ErrorCode.QUOTA_STORAGE_EXCEEDED, "平台保留存储配额不足", http_status=429
        )

    if task.initial_message_id is None:  # uploading 恒有初始消息；防御性收口
        raise _transition_conflict("任务缺少初始消息")
    round_id = uuid7()
    db.add(
        TaskRound(
            id=round_id,
            task_id=task.id,
            owner_id=owner_uuid,
            source_message_id=task.initial_message_id,
            state="pending",
            attempt=0,
        )
    )
    assert_transition("uploading", "queued")
    flipped = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == "uploading")
        .values(status="queued")
        .execution_options(synchronize_session=False)
    )
    if flipped.rowcount == 0:  # FOR UPDATE 行锁在握，理论不可达——条件仲裁双保险
        raise _transition_conflict()
    seq = await _allocate_event_sequence(db, task.id, 2)
    _add_event(
        db,
        task_id=task.id,
        owner_id=owner_uuid,
        sequence=seq,
        event_type="status_changed",
        payload={"status": "queued"},
    )
    _add_event(
        db,
        task_id=task.id,
        owner_id=owner_uuid,
        sequence=seq + 1,
        event_type="round_queued",
        payload={"round_id": str(round_id)},
        round_id=round_id,
    )
    await db.flush()
    view = await _load_task_view(db, task.id)
    return {
        "task": view,
        "manifest_sha256": manifest_sha256,
        "round_id": str(round_id),
        "event_sequence": seq + 1,
    }


# ---------------------------------------------------------------------------
# 终态意图与终态化
# ---------------------------------------------------------------------------


async def _request_terminal_while_running(
    db: AsyncSession, task: Task, owner_id: _uuid.UUID, *, terminal: str
) -> dict:
    """running 分支（D19）：活跃轮条件 UPDATE→cancelling + tasks.pending_terminal
    写位；rowcount=0（已并发收口/无活跃轮）→ 200 形当前视图、不释放（防与 settle
    双释放）。状态翻转由轮收口事务（executor/reclaim）读 pending_terminal 决定并
    清列——本分支不改任务状态、不写事件。

    返回 ``{"task"}``（200 载荷）或 ``{"task", "round": {"state": "cancelling"}}``
    （202 载荷，D14）。"""
    round_cancel = await db.execute(
        update(TaskRound)
        .where(TaskRound.task_id == task.id, TaskRound.state.in_(_ACTIVE_ROUND_STATES))
        .values(state="cancelling")
        .execution_options(synchronize_session=False)
    )
    if round_cancel.rowcount == 0:
        return {"task": await _load_task_view(db, task.id)}
    intent = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == "running")
        .values(pending_terminal=terminal)
        .execution_options(synchronize_session=False)
    )
    if intent.rowcount == 0:  # 行锁在握，理论不可达——条件仲裁双保险
        return {"task": await _load_task_view(db, task.id)}
    await db.flush()
    return {"task": await _load_task_view(db, task.id), "round": {"state": "cancelling"}}


async def _terminalize_queued(
    db: AsyncSession, task: Task, owner_id: _uuid.UUID, *, terminal: str, reason: str | None
) -> dict:
    """queued 分支（Sup §1.2:29-30）：pending round 条件 UPDATE→cancelled + 任务
    终态化 + status_changed/round_cancelled 事件 + 任务级释放（active；
    task_root 保留 7 天）。abort 与 complete-queued 直接边共用（D19）。"""
    cancelled = await db.execute(
        update(TaskRound)
        .where(TaskRound.task_id == task.id, TaskRound.state == "pending")
        .values(state="cancelled")
        .execution_options(synchronize_session=False)
    )
    assert_transition("queued", terminal)
    flipped = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == "queued")
        .values(status=terminal, abort_reason=reason)
        .execution_options(synchronize_session=False)
    )
    if flipped.rowcount == 0:  # 行锁在握，理论不可达——条件仲裁双保险
        raise _transition_conflict()
    step = 1 + (1 if cancelled.rowcount else 0)
    seq = await _allocate_event_sequence(db, task.id, step)
    _add_event(
        db,
        task_id=task.id,
        owner_id=owner_id,
        sequence=seq,
        event_type="status_changed",
        payload=_status_payload(terminal, reason),
    )
    if cancelled.rowcount:
        _add_event(
            db,
            task_id=task.id,
            owner_id=owner_id,
            sequence=seq + 1,
            event_type="round_cancelled",
            payload={"reason": reason} if reason is not None else {},
        )
    await db.flush()
    await release_task_holdings(db, task_id=str(task.id), owner_id=str(owner_id))
    return {"task": await _load_task_view(db, task.id)}


async def _finalize_terminal(
    db: AsyncSession, task: Task, owner_id: _uuid.UUID, *, terminal: str, reason: str | None
) -> dict:
    """直接终态化（ready 分支；uploading/终态在此被 assert_transition 以 409 拒）：
    条件 UPDATE 仲裁 + status_changed 事件 + 任务级释放。"""
    assert_transition(task.status, terminal)
    flipped = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == task.status)
        .values(status=terminal, abort_reason=reason)
        .execution_options(synchronize_session=False)
    )
    if flipped.rowcount == 0:  # 行锁在握，理论不可达——条件仲裁双保险
        raise _transition_conflict()
    seq = await _allocate_event_sequence(db, task.id)
    _add_event(
        db,
        task_id=task.id,
        owner_id=owner_id,
        sequence=seq,
        event_type="status_changed",
        payload=_status_payload(terminal, reason),
    )
    await db.flush()
    await release_task_holdings(db, task_id=str(task.id), owner_id=str(owner_id))
    return {"task": await _load_task_view(db, task.id)}


async def abort_task(db: AsyncSession, *, owner_id: str, task_id: str) -> dict:
    """用户取消（Sup §1.2:29 + D19）。

    - ready：直接终态化 aborted(user_cancel)（200 形）
    - queued：pending round→cancelled + aborted(user_cancel) + 对称释放（200 形）
    - running：pending_terminal='aborted' + 活跃轮→cancelling（202 形；rowcount=0
      已收口→200 形当前视图不释放）
    - uploading/终态：assert_transition → TASK_INVALID_TRANSITION 409（uploading
      用 DELETE 终态化；其余终态返回 409）
    """
    task = await _lock_task(db, task_id)
    owner_uuid = _parse_id(owner_id, "owner_id")
    if task.status == "running":
        return await _request_terminal_while_running(db, task, owner_uuid, terminal="aborted")
    if task.status == "queued":
        return await _terminalize_queued(
            db, task, owner_uuid, terminal="aborted", reason="user_cancel"
        )
    return await _finalize_terminal(db, task, owner_uuid, terminal="aborted", reason="user_cancel")


async def complete_task(db: AsyncSession, *, owner_id: str, task_id: str) -> dict:
    """优雅收尾（Sup §1.2:30 + D19）。

    - ready→completed（200 形，释放）
    - queued→completed：pending round→cancelled + completed + 释放（200 形；直接
      边不经过 cancelling）
    - running→pending_terminal='completed' + cancelling（202 形；rowcount=0 已
      收口→200 形）
    - uploading/终态→409
    """
    task = await _lock_task(db, task_id)
    owner_uuid = _parse_id(owner_id, "owner_id")
    if task.status == "running":
        return await _request_terminal_while_running(db, task, owner_uuid, terminal="completed")
    if task.status == "queued":
        return await _terminalize_queued(db, task, owner_uuid, terminal="completed", reason=None)
    return await _finalize_terminal(db, task, owner_uuid, terminal="completed", reason=None)


async def delete_task(db: AsyncSession, *, owner_id: str, task_id: str) -> dict:
    """删除（Sup §1.2:31 + D14）：任意非 deleted→deleted。

    活跃轮：running → cancelling + pending_terminal='deleted'；queued 的 pending →
    直接取消。全部 reservation released（RETURNING 闸门退账）+ 存储账全退（下限
    谓词）。**账面先行**——物理删 task-storage 由调用方 post-commit 兜底（残余归
    sweep_terminal_cleanup；本函数不做磁盘 I/O）。已删除任务统一 404（幂等重放由
    路由层 Idempotency 记录承担）。
    """
    task = await _lock_task(db, task_id)
    owner_uuid = _parse_id(owner_id, "owner_id")
    assert_transition(task.status, "deleted")
    round_cancelled = False
    if task.status == "running":
        # 活跃轮（含前序 abort 留下的 cancelling）统一 cancelling + 意图位覆盖为
        # deleted（覆盖 abort 写下的 'aborted'，轮收口读列终裁）
        await db.execute(
            update(TaskRound)
            .where(TaskRound.task_id == task.id, TaskRound.state.in_(_ACTIVE_ROUND_STATES))
            .values(state="cancelling")
            .execution_options(synchronize_session=False)
        )
        await db.execute(
            update(Task)
            .where(Task.id == task.id, Task.status == "running")
            .values(pending_terminal="deleted")
            .execution_options(synchronize_session=False)
        )
    elif task.status == "queued":
        round_cancelled = bool(
            (
                await db.execute(
                    update(TaskRound)
                    .where(TaskRound.task_id == task.id, TaskRound.state == "pending")
                    .values(state="cancelled")
                    .execution_options(synchronize_session=False)
                )
            ).rowcount
        )
    flipped = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == task.status)
        .values(status="deleted")
        .execution_options(synchronize_session=False)
    )
    if flipped.rowcount == 0:  # 行锁在握，理论不可达——条件仲裁双保险
        raise _transition_conflict()
    seq = await _allocate_event_sequence(db, task.id, 1 + (1 if round_cancelled else 0))
    _add_event(
        db,
        task_id=task.id,
        owner_id=owner_uuid,
        sequence=seq,
        event_type="status_changed",
        payload={"status": "deleted"},
    )
    if round_cancelled:
        _add_event(
            db,
            task_id=task.id,
            owner_id=owner_uuid,
            sequence=seq + 1,
            event_type="round_cancelled",
            payload={},
        )
    await db.flush()
    # 账面先行：status 已落 deleted → 全清分档（全 reservation + 存储全退）
    await release_task_holdings(db, task_id=str(task.id), owner_id=str(owner_uuid))
    await db.flush()
    return {"task": {"id": str(task.id), "status": "deleted"}}


# ---------------------------------------------------------------------------
# 消息发送（Phase 6 T8b：ready 门 + 活跃轮闸 + 预算校验）
# ---------------------------------------------------------------------------

# 活跃轮占线的重试间隔（V1 task_round_lock 429 同值语义；Retry-After 载体）
_ROUND_BUSY_RETRY_AFTER = "5"


def _round_busy() -> AgentCraftError:
    """429 TASK_ROUND_BUSY（活跃/排队轮占线；Retry-After 随错误头透传）。"""
    return AgentCraftError(
        ErrorCode.TASK_ROUND_BUSY,
        "当前一轮回复仍在进行，请稍后再发送",
        http_status=429,
        headers={"Retry-After": _ROUND_BUSY_RETRY_AFTER},
    )


def _is_active_round_conflict(exc: IntegrityError) -> bool:
    """IntegrityError 是否来自 one_active_round_per_task 部分唯一索引（约束名与
    Database Design §3 一字不差；asyncpg UniqueViolationError 携带
    constraint_name，缺失时退化到异常串匹配）。"""
    orig = getattr(exc, "orig", None)
    name = str(getattr(orig, "constraint_name", "") or "")
    return "one_active_round_per_task" in (name or str(orig or exc))


def _publish_frames_after_commit(
    db: AsyncSession, streams: TaskStreamRegistry, task_id: str, frames: list[dict]
) -> None:
    """send_message 三事件 post-commit 推帧（Phase 8 T5，§9.10.10）：
    after_commit 一次性监听（task_executor._schedule_storage_cleanup 同型）——
    事务内只捕获帧载荷不动 streams；publish 在调用方 owner_session COMMIT 成功
    后触发，回滚不触发（无幻影帧，D16 会话边界纪律）。"""

    def _publish(*_args) -> None:
        for frame in frames:
            streams.publish(task_id, frame)

    event.listen(db.sync_session, "after_commit", _publish, once=True)


async def send_message(
    db: AsyncSession,
    *,
    owner_id: str,
    task_id: str,
    content: str,
    streams: TaskStreamRegistry | None = None,
) -> dict:
    """发送下一条用户消息（Sup §1.2:25，Phase 6 T8b）。

    门序（幂等由路由层先裁——命中重放无论当前状态，见 tasks.py 模块注记）：
    活跃轮预检（429 TASK_ROUND_BUSY + Retry-After，覆盖 queued/running 及一切
    活跃轮形态）→ ready 门（其余状态 409 TASK_INVALID_TRANSITION）→ 预算校验
    （D7d，与 create 同一 ``_validate_initial_message``）→ 写事务：message 落库
    （sequence 原子分配）+ message_saved → assert_transition(ready→queued) +
    条件翻转（行锁在握）+ status_changed → INSERT round(pending,
    source_message_id) + round_queued。

    one_active_round_per_task 部分唯一索引即并发闸门：预检漏网的并发双发在
    flush 处以 IntegrityError 浮出，按约束名映射同一 429（事务随 owner_session
    回滚——无半账、不落幂等记录，客户端可原 key 重试）。

    ``streams``（Phase 8 T5）：实时注册表在位时，三事件
    （message_saved/status_changed/round_queued）经 after_commit 监听在事务提交
    后推帧（本函数不 commit——publish 由提交边界触发；本服务不直接触碰 streams）。

    返回 ``{"message": {"id", "event_sequence"}, "event_sequence": round_queued
    序, "round_id"}``（路由层包 202 ``{data: ...}`` 信封）。
    """
    task = await _lock_task(db, task_id)
    owner_uuid = _parse_id(owner_id, "owner_id")
    active = (
        await db.execute(
            select(TaskRound.id)
            .where(TaskRound.task_id == task.id, TaskRound.state.in_(_ACTIVE_ROUND_STATES))
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None:
        raise _round_busy()
    if task.status != "ready":
        raise _transition_conflict("任务当前状态不可发送消息")
    _validate_initial_message(content)

    seq_m = await _allocate_event_sequence(db, task.id)
    mid = uuid7()
    db.add(
        TaskMessage(
            id=mid,
            task_id=task.id,
            owner_id=owner_uuid,
            event_sequence=seq_m,
            author="user",
            content=content,
        )
    )
    _add_event(
        db,
        task_id=task.id,
        owner_id=owner_uuid,
        sequence=seq_m,
        event_type="message_saved",
        payload={"message_id": str(mid), "event_sequence": seq_m, "author": "user"},
        message_id=mid,
    )
    assert_transition("ready", "queued")
    flipped = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == "ready")
        .values(status="queued")
        .execution_options(synchronize_session=False)
    )
    if flipped.rowcount == 0:  # FOR UPDATE 行锁在握，理论不可达——条件仲裁双保险
        raise _transition_conflict()
    seq_r = await _allocate_event_sequence(db, task.id, 2)
    _add_event(
        db,
        task_id=task.id,
        owner_id=owner_uuid,
        sequence=seq_r,
        event_type="status_changed",
        payload={"status": "queued"},
    )
    round_id = uuid7()
    db.add(
        TaskRound(
            id=round_id,
            task_id=task.id,
            owner_id=owner_uuid,
            source_message_id=mid,
            state="pending",
            attempt=0,
        )
    )
    try:
        # 唯一活跃索引在此浮出并发冲突（429 映射；其余 IntegrityError 照常上抛）
        await db.flush()
    except IntegrityError as exc:
        if _is_active_round_conflict(exc):
            raise _round_busy() from exc
        raise
    _add_event(
        db,
        task_id=task.id,
        owner_id=owner_uuid,
        sequence=seq_r + 1,
        event_type="round_queued",
        payload={"round_id": str(round_id)},
        round_id=round_id,
    )
    await db.flush()
    if streams is not None:
        _publish_frames_after_commit(
            db,
            streams,
            str(task.id),
            [
                {
                    "type": "message_saved",
                    "message_id": str(mid),
                    "event_sequence": seq_m,
                    "author": "user",
                },
                status_frame(seq_r, "queued"),
                queued_frame(seq_r + 1, round_id),
            ],
        )
    return {
        "message": {"id": str(mid), "event_sequence": seq_m},
        "event_sequence": seq_r + 1,
        "round_id": str(round_id),
    }
