"""审核发布服务（Phase 4 裁决 D1/D4/D5/D13/D14/D23）：admin 引擎，签名冻结供 Phase 7。

锁序纪律（D14，approve/reject/takedown 一致）：单事务内先锁实体行（FOR UPDATE），
**锁获得后重读 revision 行（with_for_update + populate_existing）再做断言**——
READ COMMITTED 下等锁期间并发事务可能已 reject/翻转状态，锁前快照不可信；
且同一会话的 identity map 默认（populate_existing=False）对二读丢弃新行值、
返回锁前旧实例，必须显式 populate_existing 才能以锁后真值覆盖，否则行锁
真落位而断言全是锁前快照（已拒 revision 可被发布）。实体行锁同时串行化「不同
revision 的并发 approve」「approve×takedown」「approve×作者编辑」。断言序（全部
基于锁后行）：owner 交叉校验（revision.owner_id == entity.owner_id，防审核队列
投毒）→ status → hash 三重比对（重算 canonical 防列值被改型 TOCTOU）→ [expert]
revision_tools enabled + skill_refs published → CAS 条件更新（0 行 = 并发抢先 →
409）。content_reviews + audit_logs 与业务变更同事务写入（Sup §6:126，审计失败
即回滚）。
调用契约：admin_db 为调用方已 begin 的 admin 会话（端点壳 Phase 7 建；
测试形态见 tests/test_v2_review_service.py::_approve）。
"""

import uuid as _uuid

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.author_service import _DOMAINS
from backend.v2.author_service import _reject_invalid_uuid as _parse_uuid
from backend.v2.content_hash import content_sha256
from backend.v2.models.content import (
    AuditLog,
    ContentReview,
    RevisionTool,
    SkillRevision,
    ToolCatalog,
)
from backend.v2.reason_gate import require_reason

RESULT_APPROVED = "approved"  # D23：词表服务层常量，不加 CHECK
RESULT_REJECTED = "rejected"

_REVIEW_DOMAINS = {
    "expert_revision": _DOMAINS["experts"],
    "skill_revision": _DOMAINS["skills"],
}


def _domain_of(target_type: str):
    try:
        return _REVIEW_DOMAINS[target_type]
    except KeyError as exc:
        raise ValueError(f"未知 target_type: {target_type}") from exc


def _unified_404() -> HTTPException:
    # 统一 404 双轨形态（NOT_FOUND 走 HTTPException.detail，非 ErrorCode 枚举；
    # 与 provider_service._provider_not_found 同款语义）
    return HTTPException(
        status_code=404, detail={"code": "NOT_FOUND", "message": "revision 不存在"}
    )


async def approve_revision(
    admin_db: AsyncSession,
    *,
    target_type: str,
    revision_id: str,
    reviewer_id: str,
    reason: str,
    request_id: str | None,
) -> dict:
    reviewer_id = str(reviewer_id)  # D26 归一（种子实返 UUID 对象）
    reason = require_reason(reason)
    dom = _domain_of(target_type)
    rid = _parse_uuid(revision_id, "revision_id")
    # 锁序第 1 步（D14）：无锁读仅用于解析实体 id；真正的串行化点是实体行
    # FOR UPDATE——锁获得后必须**重读 revision 行**（READ COMMITTED 下等锁期间
    # 并发 reject/状态翻转可能已提交，锁前快照不可信，否则已拒 revision 可被发布）
    revision = (
        await admin_db.execute(select(dom.revision).where(dom.revision.id == rid))
    ).scalar_one_or_none()
    if revision is None:
        raise _unified_404()
    entity = (
        await admin_db.execute(
            select(dom.entity)
            .where(dom.entity.id == getattr(revision, dom.fk_field))
            .with_for_update()
        )
    ).scalar_one()  # FK 保证存在
    revision = (  # 锁后重读（with_for_update + populate_existing：identity map
        # 对同会话二读默认丢弃新行值返回锁前旧实例，须显式覆盖为锁后真值）
        await admin_db.execute(
            select(dom.revision)
            .where(dom.revision.id == rid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if revision.owner_id != entity.owner_id:  # D14 交叉校验：跨作者内容注入断言
        raise AgentCraftError(
            ErrorCode.REVIEW_PENDING, "revision 归属与实体所有者不一致", http_status=409
        )
    if revision.status != "pending_review":
        raise AgentCraftError(
            ErrorCode.REVIEW_PENDING, "revision 不处于待审核状态", http_status=409
        )
    recomputed = content_sha256(revision.content_json)
    if recomputed != revision.content_sha256:  # 三重比对之重算值（防 TOCTOU）
        raise AgentCraftError(
            ErrorCode.REVIEW_PENDING,
            "revision 内容与哈希不一致，审核绑定失效",
            http_status=409,
        )
    previous = entity.published_revision_id
    if dom is _DOMAINS["experts"]:
        await _assert_tools_enabled(admin_db, revision.id)
        await _assert_skill_refs_published(admin_db, revision.content_json)
    # CAS：条件更新指针 + 实体状态（D4）
    cas = await admin_db.execute(
        update(dom.entity)
        .where(
            dom.entity.id == entity.id,
            dom.entity.published_revision_id.is_not_distinct_from(previous),
        )
        .values(published_revision_id=revision.id, status="published")
        .execution_options(synchronize_session=False)
    )
    if cas.rowcount == 0:  # 并发抢先（理论上实体行锁后不可达，双保险）
        raise AgentCraftError(
            ErrorCode.REVIEW_PENDING, "发布指针已被并发操作改变，请重试", http_status=409
        )
    revision.status = "published"
    if previous is not None and previous != revision.id:
        old = (
            await admin_db.execute(select(dom.revision).where(dom.revision.id == previous))
        ).scalar_one()
        old.status = "archived"  # 既有任务快照引用不受影响（FK RESTRICT，行不删除）
    admin_db.add(
        ContentReview(
            target_type=target_type,
            target_revision_id=revision.id,
            content_sha256=recomputed,
            result=RESULT_APPROVED,
            reviewer_id=_uuid.UUID(reviewer_id),
        )
    )
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(reviewer_id),
            action=f"{dom.entity_type}.approve_publish",
            target_type=target_type,
            target_id=revision.id,
            reason=reason,
            request_id=request_id,
            detail={
                "previous_published_revision_id": str(previous) if previous else None,
                "new_published_revision_id": str(revision.id),
                "content_sha256": recomputed,
                # synchronize_session=False → ORM 属性保持 CAS 前旧值，恰为 before 语义
                "entity_status_before": entity.status,
            },
        )
    )
    await admin_db.flush()
    return {
        "entity_id": str(entity.id),
        "entity_status": "published",  # 字面量（勿读 CAS 后的 ORM 属性）
        "published_revision_id": str(revision.id),
        "previous_published_revision_id": str(previous) if previous else None,
        "revision_no": revision.revision_no,
        "content_sha256": recomputed,
    }


async def reject_revision(
    admin_db: AsyncSession,
    *,
    target_type: str,
    revision_id: str,
    reviewer_id: str,
    reason: str,
    request_id: str | None,
) -> dict:
    reviewer_id = str(reviewer_id)  # D26 归一
    reason = require_reason(reason)
    dom = _domain_of(target_type)
    rid = _parse_uuid(revision_id, "revision_id")
    revision = (
        await admin_db.execute(select(dom.revision).where(dom.revision.id == rid))
    ).scalar_one_or_none()
    if revision is None:
        raise _unified_404()
    entity = (  # 锁序一致：实体行锁（reject 不改实体，但防交叉死锁）
        await admin_db.execute(
            select(dom.entity)
            .where(dom.entity.id == getattr(revision, dom.fk_field))
            .with_for_update()
        )
    ).scalar_one()
    revision = (  # 锁后重读（同 approve——with_for_update + populate_existing，
        # identity map 默认返回锁前旧实例，锁前快照不可信）
        await admin_db.execute(
            select(dom.revision)
            .where(dom.revision.id == rid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if revision.owner_id != entity.owner_id:  # 交叉校验（D14，与 approve 同款）
        raise AgentCraftError(
            ErrorCode.REVIEW_PENDING, "revision 归属与实体所有者不一致", http_status=409
        )
    if revision.status != "pending_review":
        raise AgentCraftError(
            ErrorCode.REVIEW_PENDING, "revision 不处于待审核状态", http_status=409
        )
    if content_sha256(revision.content_json) != revision.content_sha256:
        raise AgentCraftError(
            ErrorCode.REVIEW_PENDING,
            "revision 内容与哈希不一致，审核绑定失效",
            http_status=409,
        )
    revision.status = "rejected"
    admin_db.add(
        ContentReview(
            target_type=target_type,
            target_revision_id=revision.id,
            content_sha256=revision.content_sha256,
            result=RESULT_REJECTED,
            reviewer_id=_uuid.UUID(reviewer_id),
        )
    )
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(reviewer_id),
            action=f"{dom.entity_type}.reject",
            target_type=target_type,
            target_id=revision.id,
            reason=reason,
            request_id=request_id,
            detail={"content_sha256": revision.content_sha256},
        )
    )
    await admin_db.flush()
    return {"revision_id": str(revision.id), "status": "rejected"}


async def _assert_tools_enabled(admin_db: AsyncSession, revision_id) -> None:
    rows = (
        await admin_db.execute(
            select(RevisionTool.tool_id, RevisionTool.version).where(
                RevisionTool.expert_revision_id == revision_id
            )
        )
    ).all()
    for tool_id, version in rows:
        enabled = (
            await admin_db.execute(
                select(ToolCatalog.enabled).where(
                    ToolCatalog.tool_id == tool_id, ToolCatalog.version == version
                )
            )
        ).scalar_one_or_none()
        if enabled is not True:  # 不存在或不启用同罚（D20/D14）
            raise AgentCraftError(
                ErrorCode.TOOL_REVOKED,
                f"工具版本未启用：{tool_id}@{version}",
                http_status=409,
            )


async def _assert_skill_refs_published(admin_db: AsyncSession, content_json) -> None:
    refs = content_json.get("skill_refs") or []
    for ref in refs:
        rid = _uuid.UUID(ref["revision_id"])
        status = (
            await admin_db.execute(select(SkillRevision.status).where(SkillRevision.id == rid))
        ).scalar_one_or_none()
        if status != "published":  # D5：引用的 skill revision 必须已发布
            raise AgentCraftError(
                ErrorCode.REVISION_NOT_PUBLISHED,
                "引用的 skill revision 未发布",
                http_status=409,
            )
