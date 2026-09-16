"""admin 审计查询与产物下载（读审计变体）服务（Phase 7 T7；Sup §6:146 + D7h）。

调用契约（同 admin_user_service）：``admin_db`` 为调用方 admin 会话——查询端点
只读直用（admin_factory 无事务，SELECT 隐式事务随会话关闭回收）；下载端点在
``admin_factory().begin()`` 事务内调用（审计行随事务提交/回滚收口）。

- ``query_audit_logs``：audit_logs 为管理面数据（app role 零授权、admin 全量
  DML，0001:1023-1026），admin 会话全表可见。六维过滤全可选（actor_id/action/
  target_type/target_id/since/until 时间窗，窗口双端闭区间）+ created_at DESC
  + id DESC 稳定序 + 分页（Sup §7 列表信封 {items,total,page,size}，1≤size≤100）；
  元数据读免 reason 免幂等（计划 T7 钉死）。since/until 接受 ISO 8601 串
  （naive 视为 UTC——asyncpg timestamptz 需 aware），非法解析统一 400
  VALIDATION_ERROR（不经 FastAPI 422，错误信封形状一致）。
- ``admin_download_artifact``（读审计变体，Sup:128 / 计划红线 §4）：先 INSERT
  AuditLog（action='task.artifact.download'，detail={task_id,file_id,sha256}）
  → **flush 成功后才**复用冻结的 ``resolve_download``（direction/state/deleted
  校验照旧，404 语义分流全在冻结函数不复制漂移）；审计写失败即拒读（异常向上
  → 端点壳事务回滚）。sha256 经窄 SELECT（output+registered 行）预取用于审计
  detail——属元数据读（非内容读），行不存在则**不写审计**、由 resolve_download
  给出统一 404（失败尝试不留审计残留；端点壳 begin() 对已写行一并回滚）。
  resolve_download 的 owner_id 仅作 UUID 解析、不参与行过滤（tasks/task_files
  的 admin_read policy USING(true) 使 admin 会话可见全量）——owner 面「他人
  404」语义在 admin 会话不成立，admin 面语义 = 可见全部有效产物，传 admin_id
  （申报：传参仅满足冻结签名的解析位，无过滤效果）。
- ``admin_get_task``/``admin_list_user_tasks``（Phase 8 T4，Sup §10.5 元数据读
  分层）：免 reason 免审计直读。快照复用冻结的 ``get_task_view``（owner 面
  D14 视图同形 + §10.3 expert/provider 展示字段；admin 会话 *_admin_read
  USING(true) 使其可见全量，owner_id 形参仅满足冻结签名——无过滤效果，
  admin_download_artifact 同款申报）；用户任务列表 status 词表过滤（词表外
  400，§9.11.3 先例）+ created_at DESC/id DESC 稳定序分页，deleted 墓碑
  不可见（owner 面同口径），未知用户统一 404 NOT_FOUND（get_user_detail
  同形信封）。
- ``admin_list_task_messages``/``admin_list_task_files``（Phase 8 T4 内容读
  分层）：``_audit_then_read`` 公共前奏——存活预取（缺失/已删除统一 404，
  **预取落空短路不落审计**，§9.11.4 先例同形）→ INSERT AuditLog（action=
  task.message.read / task.file_list.read，detail={task_id} 零内容材料）→
  **flush 成功后才进读取面**（写失败异常向上 → 端点壳事务回滚拒读）；messages
  复用冻结 ``list_task_messages``（升序全量正文），files 走五键元数据窄 SELECT
  （id/file_name/sha256/size_bytes/state，Phase 9 T2 §10.11(b) 补 id；无内容字节无
  storage_key；deleted 墓碑不可见；created_at/id 稳定序），direction 词表校验先于审计
"""

import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.v2.models import AuditLog, Task, TaskFile, User
from backend.v2.models.tasking import TASK_STATUSES
from backend.v2.task_artifacts import resolve_download
from backend.v2.task_views import (
    _parse_id,
    _require_live_task,
    get_task_view,
    list_task_messages,
)

__all__ = [
    "admin_download_artifact",
    "admin_get_task",
    "admin_list_task_files",
    "admin_list_task_messages",
    "admin_list_user_tasks",
    "query_audit_logs",
]

_LIST_PAGE_MAX = 100


def _validation(message: str) -> HTTPException:
    # VALIDATION_ERROR 为 V1 约定形状字面（不经 ErrorCode 注册表，admin_user_service 同款）
    return HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": message})


def _parse_uuid_or_400(value: str, field: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(str(value))
    except ValueError:
        raise _validation(f"{field} 须为合法 UUID") from None


def _parse_ts_or_400(value: str, field: str) -> datetime:
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        raise _validation(f"{field} 须为 ISO 8601 时间戳") from None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)  # naive 视为 UTC（timestamptz 需 aware）
    return ts


def _audit_brief(row: AuditLog) -> dict:
    """audit_logs 行 → 查询项（全列出队：管理台渲染面；detail 原样 JSONB）。"""
    return {
        "id": str(row.id),
        "actor_id": str(row.actor_id) if row.actor_id is not None else None,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": str(row.target_id) if row.target_id is not None else None,
        "reason": row.reason,
        "request_id": row.request_id,
        "detail": row.detail,
        "created_at": row.created_at.isoformat(),
    }


async def query_audit_logs(
    admin_db: AsyncSession,
    *,
    actor_id: str | None,
    action: str | None,
    target_type: str | None,
    target_id: str | None,
    since: str | None,
    until: str | None,
    page: int,
    size: int,
) -> dict:
    """审计查询（六维过滤全可选 + created_at DESC + id DESC 稳定序分页）。

    只读免 reason（计划 T7）；非法分页/非 UUID actor_id、target_id/非 ISO
    since、until → 400 VALIDATION_ERROR。返回 {items,total,page,size}。
    """
    if page < 1 or size < 1 or size > _LIST_PAGE_MAX:
        raise _validation("分页参数非法（page≥1，1≤size≤100）")
    conds = []
    if actor_id is not None:
        conds.append(AuditLog.actor_id == _parse_uuid_or_400(actor_id, "actor_id"))
    if action is not None:
        conds.append(AuditLog.action == action)
    if target_type is not None:
        conds.append(AuditLog.target_type == target_type)
    if target_id is not None:
        conds.append(AuditLog.target_id == _parse_uuid_or_400(target_id, "target_id"))
    if since is not None:
        conds.append(AuditLog.created_at >= _parse_ts_or_400(since, "since"))
    if until is not None:
        conds.append(AuditLog.created_at <= _parse_ts_or_400(until, "until"))
    total = int(
        (
            await admin_db.execute(select(func.count()).select_from(AuditLog).where(*conds))
        ).scalar_one()
    )
    rows = (
        (
            await admin_db.execute(
                select(AuditLog)
                .where(*conds)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_audit_brief(row) for row in rows],
        "total": total,
        "page": page,
        "size": size,
    }


async def admin_download_artifact(
    admin_db: AsyncSession,
    storage,
    *,
    admin_id: str,
    task_id: str,
    file_id: str,
    reason: str,
    request_id: str | None,
) -> tuple[Path, dict]:
    """admin 产物下载解析（读审计变体）：先审计（flush 落定）后复用冻结
    resolve_download；审计写失败异常向上（拒读，端点壳事务回滚）。

    审计行仅对**存在且可解析的产物面行**（direction='output' AND
    state='registered'）预写——sha256 预取 SELECT 落空（缺失/非产物面）时不写
    审计，统一 404 由 resolve_download 给出；命中后若 resolve_download 仍失败
    （如任务已删除），端点壳 begin() 回滚已写审计行，无残留。返回 (物理路径,
    {file_name, sha256, size_bytes})——物理路径为 artifacts 登记副本
    （download_headers 由路由层消费）。
    """
    tid = _parse_id(task_id, "task_id")
    fid = _parse_id(file_id, "file_id")
    sha256 = (
        await admin_db.execute(
            select(TaskFile.sha256).where(
                TaskFile.id == fid,
                TaskFile.task_id == tid,
                TaskFile.direction == "output",
                TaskFile.state == "registered",
            )
        )
    ).scalar_one_or_none()
    if sha256 is not None:
        admin_db.add(
            AuditLog(
                actor_id=_uuid.UUID(admin_id),
                action="task.artifact.download",
                target_type="task",
                target_id=tid,
                reason=reason,
                request_id=request_id,
                detail={"task_id": str(tid), "file_id": str(fid), "sha256": sha256},
            )
        )
        # 读审计红线：flush 成功后才允许进入读取面；写失败异常向上即拒读
        await admin_db.flush()
    return await resolve_download(
        admin_db, storage, owner_id=admin_id, task_id=task_id, file_id=file_id
    )


# ---------------------------------------------------------------------------
# D8 admin 读端点服务（Phase 8 T4；Sup §10.5——元数据读直读 / 内容读先审计后读）
# ---------------------------------------------------------------------------


async def _audit_then_read(
    admin_db: AsyncSession,
    *,
    task_id: str,
    action: str,
    admin_id: str,
    reason: str,
    request_id: str | None,
) -> _uuid.UUID:
    """内容读公共前奏（#17 admin_download_artifact 同款红线）：

    1. 存活预取（``_require_live_task``：缺失/他人 RLS 0 行/已删除统一 404）——
       预取落空短路，**不写审计**（失败尝试零残留，§9.11.4 先例同形）；
    2. INSERT AuditLog（detail={task_id} 零内容材料）→ **flush 成功后才允许
       进入读取面**；写失败异常向上 → 端点壳事务回滚拒读。

    返回规范化 task UUID（读取面复用，免二次路径段解析）。
    """
    tid = await _require_live_task(admin_db, task_id)
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action=action,
            target_type="task",
            target_id=tid,
            reason=reason,
            request_id=request_id,
            detail={"task_id": str(tid)},
        )
    )
    # 读审计红线：flush 成功后才允许进入读取面；写失败异常向上即拒读
    await admin_db.flush()
    return tid


async def admin_list_task_messages(
    admin_db: AsyncSession,
    *,
    task_id: str,
    after: int,
    limit: int,
    admin_id: str,
    reason: str,
    request_id: str | None = None,
) -> list[dict]:
    """admin 任务消息读（Sup §10.5 内容读）：先审计（action=task.message.read）
    后复用冻结 ``list_task_messages``——event_sequence 升序全量正文，after 游标
    + limit 截断（默认 50 上限 200 由路由层 Query 门校验）。owner_id 形参仅满足
    冻结签名（解析位，无过滤效果——admin_read USING(true)）。"""
    tid = await _audit_then_read(
        admin_db,
        task_id=task_id,
        action="task.message.read",
        admin_id=admin_id,
        reason=reason,
        request_id=request_id,
    )
    return await list_task_messages(
        admin_db, owner_id=admin_id, task_id=str(tid), after=after, limit=limit
    )


async def _task_file_meta_rows(
    admin_db: AsyncSession, *, tid: _uuid.UUID, direction: str
) -> list[dict]:
    """任务文件元数据窄 SELECT（Sup §10.11(b) 五键 id/file_name/sha256/size_bytes/
    state——无内容字节无 storage_key；deleted 墓碑不可见；created_at ASC +
    id ASC 稳定序，task_file_service.list_files 同构）。独立模块级函数：注入
    实证的 sentinel 打点位（test_v2_admin_reads）。"""
    rows = (
        await admin_db.execute(
            select(
                TaskFile.id,
                TaskFile.file_name,
                TaskFile.sha256,
                TaskFile.size_bytes,
                TaskFile.state,
            )
            .where(
                TaskFile.task_id == tid,
                TaskFile.direction == direction,
                TaskFile.state != "deleted",
            )
            .order_by(TaskFile.created_at.asc(), TaskFile.id.asc())
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "file_name": r.file_name,
            "sha256": r.sha256,
            "size_bytes": int(r.size_bytes),
            "state": r.state,
        }
        for r in rows
    ]


async def admin_list_task_files(
    admin_db: AsyncSession,
    *,
    task_id: str,
    direction: str | None,
    admin_id: str,
    reason: str,
    request_id: str | None = None,
) -> list[dict]:
    """admin 任务文件元数据读（Sup §10.5 内容读）：direction 词表校验（缺失/
    词表外 400，先于审计——校验失败零审计）→ 先审计（action=task.file_list.read）
    后元数据读取。"""
    if direction is None or direction not in ("input", "output"):
        raise _validation("direction 必须为 input 或 output")
    tid = await _audit_then_read(
        admin_db,
        task_id=task_id,
        action="task.file_list.read",
        admin_id=admin_id,
        reason=reason,
        request_id=request_id,
    )
    return await _task_file_meta_rows(admin_db, tid=tid, direction=direction)


async def admin_get_task(admin_db: AsyncSession, *, task_id: str, admin_id: str) -> dict:
    """admin 任务快照（Sup §10.5 元数据读，免 reason 免审计）：复用冻结
    ``get_task_view``——owner 面 D14 视图同形 + §10.3 expert/provider 展示字段；
    缺失/他人（admin_read 可见全量，仅存于缺失/已删除）/已删除统一 404 内建。
    owner_id 形参传 admin_id 仅满足冻结签名（解析位，无过滤效果——申报同
    admin_download_artifact）。"""
    return await get_task_view(admin_db, owner_id=admin_id, task_id=task_id)


async def admin_list_user_tasks(
    admin_db: AsyncSession,
    *,
    user_id: str,
    status: str | None,
    page: int,
    size: int,
) -> dict:
    """admin 用户任务列表（Sup §10.5 元数据读，免 reason 免审计）：status 词表
    过滤（词表外 400 VALIDATION_ERROR，§9.11.3 先例）+ created_at DESC/id DESC
    稳定序分页；deleted 墓碑不可见（owner 面同口径）；未知用户统一 404
    NOT_FOUND（admin_user_service.get_user_detail 同形信封）。返回
    {items,total,page,size}，item 四键 id/status/abort_reason/created_at。"""
    if page < 1 or size < 1 or size > _LIST_PAGE_MAX:
        raise _validation("分页参数非法（page≥1，1≤size≤100）")
    if status is not None and status not in TASK_STATUSES:
        raise _validation("status 词表非法（" + "/".join(TASK_STATUSES) + "）")
    uid = _parse_id(user_id, "user_id")
    exists = (await admin_db.execute(select(User.id).where(User.id == uid))).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "用户不存在"})
    conds = [Task.owner_id == uid, Task.status != "deleted"]
    if status is not None:
        conds.append(Task.status == status)
    total = int(
        (await admin_db.execute(select(func.count()).select_from(Task).where(*conds))).scalar_one()
    )
    rows = (
        await admin_db.execute(
            select(Task.id, Task.status, Task.abort_reason, Task.created_at)
            .where(*conds)
            .order_by(Task.created_at.desc(), Task.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    return {
        "items": [
            {
                "id": str(r.id),
                "status": r.status,
                "abort_reason": r.abort_reason,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "size": size,
    }
