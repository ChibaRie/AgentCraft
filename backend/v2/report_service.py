"""举报域服务（Phase 4 裁决 D10/D11/D15/D16）：创建走 owner 引擎，处置走 admin 引擎。

create_report 门序：幂等 begin → owner_session（目标校验 → INSERT，reporter_id 由
owner 上下文写——RLS WITH CHECK 兜底）。resolve_report（D13 冻结签名）：锁 report
→ 状态门 → dismiss / takedown 分派；takedown 按实体当前 published_revision_id
指针反向处置（D15），锁序与 approve 一致（先实体行 FOR UPDATE）。
"""

import uuid as _uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.author_service import _DOMAINS, _reject_invalid_uuid
from backend.v2.idempotency import begin, store, subject_user
from backend.v2.ids import uuid7
from backend.v2.models.content import AuditLog, Report
from backend.v2.models.tasking import TaskMessage
from backend.v2.runtime import V2Runtime, owner_session

_VALID_ACTIONS = ("dismiss", "takedown_revision")
_MESSAGE_TAKEDOWN_MESSAGE = "message 举报不支持 takedown_revision，请使用 dismiss"


@dataclass(frozen=True)
class Replay:
    status_code: int
    response_json: dict


def _reason_gate(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise AgentCraftError(
            ErrorCode.ADMIN_REASON_REQUIRED, "管理员操作必须提供 reason", http_status=400
        )
    return reason.strip()


async def create_report(
    runtime: V2Runtime,
    *,
    reporter_id: str,
    payload: dict,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    reporter_id = str(reporter_id)  # D26 归一（种子实返 UUID 对象）
    route = "/api/v2/reports"
    async with runtime.app_factory() as db:
        replay = await begin(
            db,
            subject_hash=subject_user(reporter_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])
    target_id = _reject_invalid_uuid(payload["target_id"], "target_id")
    async with owner_session(runtime, reporter_id) as db:
        await _validate_target(db, payload["target_type"], target_id)
        report = Report(
            id=uuid7(),
            reporter_id=_uuid.UUID(reporter_id),
            target_type=payload["target_type"],
            target_id=target_id,
            target_revision_hash=payload.get("target_revision_hash"),
            status="open",
            reason=payload["reason"],
        )
        db.add(report)
        await db.flush()
        await db.refresh(report)  # server_default created_at 落地后再出参
        body = {"data": _report_brief(report)}
        await store(
            db,
            subject_hash=subject_user(reporter_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=body,
        )
    return body["data"]


async def _validate_target(db, target_type: str, target_id: _uuid.UUID) -> None:
    """D10 矩阵：RLS 0 行（缺失/越权/已归档）→ 404；可见但不可举报 → 400。"""
    if target_type in ("expert_revision", "skill_revision"):
        dom = _DOMAINS["experts" if target_type == "expert_revision" else "skills"]
        row = (
            await db.execute(select(dom.revision.status).where(dom.revision.id == target_id))
        ).scalar_one_or_none()
        if row is None:  # 缺失 / 他人非公开 / 已被替代 → 对本上下文不可见
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "举报目标不存在"}
            )
        if row != "published":  # 本人 draft/pending 可见但不可举报
            raise HTTPException(
                status_code=400,
                detail={"code": "REPORT_INVALID_TARGET", "message": "仅可举报已公开的内容版本"},
            )
    elif target_type == "message":
        row = (
            await db.execute(select(TaskMessage.author).where(TaskMessage.id == target_id))
        ).scalar_one_or_none()
        if row is None:  # 他人消息被 owner RLS 过滤 → 统一 404（D11）
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "举报目标不存在"}
            )
        if row != "assistant":  # tool/user 消息不可举报（D11）
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "REPORT_INVALID_TARGET",
                    "message": "仅可举报任务中的 AI 回复消息",
                },
            )
    else:
        raise HTTPException(
            status_code=400, detail={"code": "VALIDATION_ERROR", "message": "target_type 非法"}
        )


def _report_brief(report: Report) -> dict:
    return {
        "id": str(report.id),
        "target_type": report.target_type,
        "target_id": str(report.target_id),
        "status": report.status,
        "reason": report.reason,
        "created_at": report.created_at.isoformat(),
    }


async def resolve_report(
    admin_db: AsyncSession,
    *,
    report_id: str,
    action: str,
    admin_id: str,
    reason: str,
    request_id: str | None,
) -> dict:
    admin_id = str(admin_id)  # D26 归一
    reason = _reason_gate(reason)
    if action not in _VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": f"action 仅支持 {_VALID_ACTIONS}（ban_author 属 Phase 7）",
            },
        )
    rid = _reject_invalid_uuid(report_id, "report_id")
    report = (
        await admin_db.execute(select(Report).where(Report.id == rid).with_for_update())
    ).scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "举报不存在"})
    if report.status != "open":  # D16
        raise AgentCraftError(ErrorCode.REPORT_ALREADY_RESOLVED, "举报已被处置", http_status=409)
    if action == "dismiss":
        report.status = "dismissed"
        admin_db.add(
            AuditLog(
                actor_id=_uuid.UUID(admin_id),
                action="report.dismiss",
                target_type="report",
                target_id=report.id,
                reason=reason,
                request_id=request_id,
            )
        )
        await admin_db.flush()
        return {"report_id": str(report.id), "status": "dismissed"}
    # ---- takedown_revision（D15）----
    if report.target_type == "message":
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": _MESSAGE_TAKEDOWN_MESSAGE},
        )
    dom = _DOMAINS["experts" if report.target_type == "expert_revision" else "skills"]
    revision = (
        await admin_db.execute(select(dom.revision).where(dom.revision.id == report.target_id))
    ).scalar_one_or_none()
    if revision is None:
        raise HTTPException(
            status_code=404, detail={"code": "NOT_FOUND", "message": "举报目标不存在"}
        )
    entity = (
        await admin_db.execute(
            select(dom.entity)
            .where(dom.entity.id == getattr(revision, dom.fk_field))
            .with_for_update()
        )
    ).scalar_one()
    current = entity.published_revision_id
    if current is None:  # D15：无公开版本可处置
        raise AgentCraftError(
            ErrorCode.REVISION_NOT_PUBLISHED, "该内容当前没有公开版本", http_status=409
        )
    published = (  # 锁后重读（review_service D14 同款）：current == report.target_id
        # 的常态下无锁首读已将该行装入 identity map，普通重读会被锁前旧实例架空，
        # 必须 with_for_update + populate_existing 才能以锁后真值覆盖
        await admin_db.execute(
            select(dom.revision)
            .where(dom.revision.id == current)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    published.status = "archived"
    entity.published_revision_id = None
    entity.status = "draft"  # D4 反向：回「无公开版本」的作者可编辑态
    report.status = "actioned"
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action="report.takedown",
            target_type="report",
            target_id=report.id,
            reason=reason,
            request_id=request_id,
            detail={
                "target_revision_id": str(current),
                "content_sha256": published.content_sha256,
                "entity_id": str(entity.id),
            },
        )
    )
    await admin_db.flush()
    return {"report_id": str(report.id), "status": "actioned", "takedown_revision_id": str(current)}
