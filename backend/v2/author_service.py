"""作者域服务（Phase 4 裁决 D5/D6/D7/D17/D21/D22）：experts/skills 同构双域。

门序（镜像 provider_service.create_provider）：
  幂等 begin（app 裸会话 own_transaction）→ 命中返回 Replay → owner_session
  单事务（entitlement 门 → 内容边界（64KiB）→ skill_refs 校验 → 实体行锁（仅编辑）
  → draft 判定（覆写 or 新建）→ hash 计算 → 落库 → store）。
draft 两段式（D7）：draft 可覆写（content_json+hash 重算）；submit 起不可变
  （0006 触发器 DB 层强制）。编辑命中非 draft 最新 revision → 新建 draft（no+1）。
零日志：不 print/log 用户内容。
"""

import json
import uuid as _uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.engine.platform_tools import PLATFORM_TOOLS
from backend.errors import AgentCraftError, ErrorCode
from backend.v2.content_autocheck import run_auto_check
from backend.v2.content_hash import ContentTooLarge, assert_content_size, content_sha256
from backend.v2.idempotency import begin, store, subject_user
from backend.v2.ids import uuid7
from backend.v2.models.content import (
    ENTITY_STATUSES,
    AuditLog,
    Expert,
    ExpertRevision,
    RevisionTool,
    Skill,
    SkillRevision,
    ToolCatalog,
)
from backend.v2.runtime import V2Runtime, owner_session

_ENTITLEMENT_SQL = (
    "SELECT 1 FROM user_entitlements "
    "WHERE user_id = CAST(:u AS uuid) AND entitlement = 'expert_author' "
    "AND revoked_at IS NULL"
)


@dataclass(frozen=True)
class Replay:
    """幂等重放载荷（§7 重放优先；端点原样返回，不携带 Set-Cookie）。"""

    status_code: int
    response_json: dict


@dataclass(frozen=True)
class _Domain:
    route_prefix: str
    entity: type
    revision: type
    fk_field: str  # revision 表的实体外键属性名
    revision_type: str  # content_reviews/reports 词表
    entity_type: str  # audit target_type 词表


_DOMAINS = {
    "experts": _Domain(
        "/api/experts", Expert, ExpertRevision, "expert_id", "expert_revision", "expert"
    ),
    "skills": _Domain("/api/skills", Skill, SkillRevision, "skill_id", "skill_revision", "skill"),
}

# harness 闸（Sup §10.6，Phase 8 T6）：平台 harness-kind 工具不进作者工具集
# （执行回调为控制面 /internal 端点，作者面不可绑定）。服务端权威，前端
# 工具多选排除项对齐。kind 来源唯一：backend.engine.platform_tools.PLATFORM_TOOLS。
_HARNESS_TOOL_KEYS = frozenset(
    key for key, tool in PLATFORM_TOOLS.items() if tool.kind == "harness"
)


def _domain(target: str) -> _Domain:
    try:
        return _DOMAINS[target]
    except KeyError as exc:
        raise ValueError(f"未知实体域: {target}") from exc


async def _assert_expert_author(db: AsyncSession, user_id: str) -> None:
    """expert_author 门（D17）：无活跃 entitlement → 403 FORBIDDEN。"""
    row = (await db.execute(text(_ENTITLEMENT_SQL), {"u": user_id})).first()
    if row is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "需要专家作者身份"},
        )


def _reject_invalid_uuid(value: str, field: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": f"{field} 不是合法 UUID"},
        ) from exc


def _reject_invalid_status(status: str | None) -> None:
    """list_entities 的 status 白名单门（brief 注记 3）：None 不过滤，词表外 → 400。"""
    if status is not None and status not in ENTITY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": "status 仅支持 draft/published/archived",
            },
        )


async def _validate_skill_refs(db: AsyncSession, user_id: str, content: dict) -> None:
    """skill_refs 校验（D5）：存在 + (own 任意状态 或 他人 published) + id 配对一致。"""
    for ref in content.get("skill_refs") or []:
        skill_id = _reject_invalid_uuid(ref["skill_id"], "skill_refs[].skill_id")
        revision_id = _reject_invalid_uuid(ref["revision_id"], "skill_refs[].revision_id")
        revision = (
            await db.execute(select(SkillRevision).where(SkillRevision.id == revision_id))
        ).scalar_one_or_none()
        if revision is None or revision.skill_id != skill_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "skill_refs 引用的 skill revision 不存在或与 skill_id 不匹配",
                },
            )
        if revision.owner_id != _uuid.UUID(user_id) and revision.status != "published":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": "skill_refs 仅可引用本人 skill revision 或他人已发布 revision",
                },
            )


def _entity_brief(dom: _Domain, entity: Any) -> dict:
    """实体行 → 出参 dict（brief 注记 4；时间戳 isoformat）。"""
    return {
        "id": str(entity.id),
        "status": entity.status,
        "published_revision_id": (
            str(entity.published_revision_id) if entity.published_revision_id is not None else None
        ),
        "created_at": entity.created_at.isoformat(),
        "updated_at": entity.updated_at.isoformat(),
    }


def _revision_brief(revision: Any) -> dict:
    """revision 行 → 出参 dict（brief 注记 4；content_json 原样，时间戳 isoformat）。"""
    return {
        "revision_id": str(revision.id),
        "revision_no": revision.revision_no,
        "status": revision.status,
        "content_sha256": revision.content_sha256,
        "content_json": revision.content_json,
        "created_at": revision.created_at.isoformat(),
        "updated_at": revision.updated_at.isoformat(),
    }


async def create_entity(
    runtime: V2Runtime,
    *,
    user_id: str,
    target: str,
    content: dict,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """创建实体 + no=1 draft revision（门序见模块 docstring；不 commit——owner_session 收口）。"""
    dom = _domain(target)
    user_id = str(user_id)  # D26：种子/HTTP 双形态归一（seed_active_user 实返 UUID 对象）
    async with runtime.app_factory() as db:
        replay = await begin(
            db,
            subject_hash=subject_user(user_id),
            route=dom.route_prefix,
            key=idem_key,
            req_hash=idem_hash,
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])
    async with owner_session(runtime, user_id) as db:
        await _assert_expert_author(db, user_id)
        try:
            assert_content_size(content)
        except ContentTooLarge as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": str(exc)},
            ) from exc
        await _validate_skill_refs(db, user_id, content)
        sha = content_sha256(content)
        entity = dom.entity(id=uuid7(), owner_id=_uuid.UUID(user_id), status="draft")
        db.add(entity)
        await db.flush()
        revision = dom.revision(
            id=uuid7(),
            **{dom.fk_field: entity.id},
            owner_id=_uuid.UUID(user_id),
            revision_no=1,
            content_json=content,
            content_sha256=sha,
            status="draft",
        )
        db.add(revision)
        await db.flush()
        await db.refresh(entity)  # server_default 时间戳落地后再出参（_brief 直接 isoformat）
        await db.refresh(revision)
        payload = {
            "data": {"entity": _entity_brief(dom, entity), "revision": _revision_brief(revision)}
        }
        await store(
            db,
            subject_hash=subject_user(user_id),
            route=dom.route_prefix,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=payload,
        )
    return payload["data"]


async def edit_entity(
    runtime: V2Runtime,
    *,
    user_id: str,
    target: str,
    entity_id: str,
    content: dict,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """编辑（draft 两段式 D7）：最新 revision 为 draft → 覆写；否则新建 draft（no+1）。"""
    dom = _domain(target)
    user_id = str(user_id)  # D26 归一
    eid = _reject_invalid_uuid(entity_id, "entity_id")
    route = f"{dom.route_prefix}/{entity_id}"
    async with runtime.app_factory() as db:
        replay = await begin(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])
    async with owner_session(runtime, user_id) as db:
        await _assert_expert_author(db, user_id)
        try:
            assert_content_size(content)
        except ContentTooLarge as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": str(exc)},
            ) from exc
        await _validate_skill_refs(db, user_id, content)
        # 实体行锁（D21）：串行化并发编辑/提审的 revision_no 分配
        entity = (
            await db.execute(select(dom.entity).where(dom.entity.id == eid).with_for_update())
        ).scalar_one_or_none()
        if entity is None:  # 跨 owner 或不存在 → 统一 404（NOT_FOUND 走 HTTPException 双轨）
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "实体不存在"}
            )
        latest_no = (
            await db.execute(
                select(func.max(dom.revision.revision_no)).where(
                    getattr(dom.revision, dom.fk_field) == eid
                )
            )
        ).scalar_one()
        latest = None
        if latest_no is not None:
            latest = (
                await db.execute(
                    select(dom.revision).where(
                        getattr(dom.revision, dom.fk_field) == eid,
                        dom.revision.revision_no == latest_no,
                    )
                )
            ).scalar_one()
        if latest is not None and latest.status == "draft":
            latest.content_json = content  # 0006 触发器允许 draft 覆写
            latest.content_sha256 = content_sha256(content)
            revision = latest
        else:
            revision = dom.revision(
                id=uuid7(),
                **{dom.fk_field: eid},
                owner_id=_uuid.UUID(user_id),
                revision_no=(latest_no or 0) + 1,
                content_json=content,
                content_sha256=content_sha256(content),
                status="draft",
            )
            db.add(revision)
        await db.flush()
        await db.refresh(entity)  # server_default 时间戳落地后再出参（_brief 直接 isoformat）
        await db.refresh(revision)
        payload = {
            "data": {"entity": _entity_brief(dom, entity), "revision": _revision_brief(revision)}
        }
        await store(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=payload,
        )
    return payload["data"]


async def list_entities(
    db: AsyncSession,
    *,
    user_id: str,
    target: str,
    status: str | None,
) -> list[dict]:
    """本人实体列表（吃调用方 owner_session 会话；D6 本人语义）。

    status=None 不过滤；词表外 → 400（注记 3）。显式 owner_id 过滤收窄 RLS
    发布可见面（他人 published 实体不可混入）；两次查询（实体 + 仅本人实体集的
    revision，in 绑定收界——不取全平台发布可见 revision）后 Python 按 fk 分组；
    每项附 revision_count / latest_revision / name（name 取最新 revision
    content_json.name，无 revision 时为 None）。
    """
    user_id = str(user_id)  # D26 归一
    _reject_invalid_status(status)
    dom = _domain(target)
    ent_query = (
        select(dom.entity)
        .where(dom.entity.owner_id == _uuid.UUID(user_id))  # D6：本人列表
        .order_by(dom.entity.created_at.asc(), dom.entity.id.asc())
    )
    if status is not None:
        ent_query = ent_query.where(dom.entity.status == status)
    entities = (await db.execute(ent_query)).scalars().all()
    by_entity: dict[Any, list[Any]] = {}
    if entities:  # 空集合跳过 revision 查询（无谓 IO 为零）
        revisions = (
            (
                await db.execute(
                    select(dom.revision)
                    .where(getattr(dom.revision, dom.fk_field).in_([ent.id for ent in entities]))
                    .order_by(dom.revision.revision_no.asc(), dom.revision.id)
                )
            )
            .scalars()
            .all()
        )
        for rev in revisions:
            by_entity.setdefault(getattr(rev, dom.fk_field), []).append(rev)
    items: list[dict] = []
    for ent in entities:
        revs = by_entity.get(ent.id, [])
        latest = revs[-1] if revs else None
        items.append(
            {
                "id": str(ent.id),
                "status": ent.status,
                "published_revision_id": (
                    str(ent.published_revision_id)
                    if ent.published_revision_id is not None
                    else None
                ),
                "revision_count": len(revs),
                "latest_revision": (
                    None
                    if latest is None
                    else {
                        "revision_id": str(latest.id),
                        "revision_no": latest.revision_no,
                        "status": latest.status,
                        "content_sha256": latest.content_sha256,
                        "updated_at": latest.updated_at.isoformat(),
                    }
                ),
                "name": latest.content_json.get("name") if latest is not None else None,
            }
        )
    return items


async def get_entity(
    db: AsyncSession,
    *,
    user_id: str,
    target: str,
    entity_id: str,
) -> dict:
    """本人实体详情 + 全部 revisions（吃调用方 owner_session 会话；统一 404）。

    D6 本人语义：显式 owner_id 过滤收窄 RLS 发布可见面——他人实体（含
    published）一律与其他不可见形态同形 404。
    """
    user_id = str(user_id)  # D26 归一
    eid = _reject_invalid_uuid(entity_id, "entity_id")
    dom = _domain(target)
    entity = (
        await db.execute(
            select(dom.entity).where(
                dom.entity.id == eid,
                dom.entity.owner_id == _uuid.UUID(user_id),  # D6：本人详情
            )
        )
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "实体不存在"})
    revisions = (
        (
            await db.execute(
                select(dom.revision)
                .where(getattr(dom.revision, dom.fk_field) == eid)
                .order_by(dom.revision.revision_no.asc(), dom.revision.id)
            )
        )
        .scalars()
        .all()
    )
    return {
        dom.entity_type: _entity_brief(dom, entity),  # {"expert"|"skill": {...}}（Interfaces 形态）
        "revisions": [_revision_brief(rev) for rev in revisions],
    }


_MAX_TOOLS = 20


async def submit_revision(
    runtime: V2Runtime,
    *,
    user_id: str,
    target: str,
    entity_id: str,
    revision_id: str,
    tools: list[dict],
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """提审（D9/D20）：draft→pending_review；hash 定格；revision_tools 落行。

    自动检查 valid=False → 400 VALIDATION_ERROR（message 附前 3 条 issue）；
    tools 校验：去重后 ≤20 且 (tool_id,version) 存在于 tool_catalog（不要求 enabled）；
    harness-kind 工具 → 400（平台工具不进作者工具集，Sup §10.6）；
    skill 域传 tools → 400。重复提审 → 409 REVIEW_PENDING（0006 触发器兜底 DB 层）。
    """
    dom = _domain(target)
    user_id = str(user_id)  # D26 归一
    eid = _reject_invalid_uuid(entity_id, "entity_id")
    rid = _reject_invalid_uuid(revision_id, "revision_id")
    route = f"{dom.route_prefix}/{entity_id}/revisions/{revision_id}/submit"
    req_hash = idem_hash
    async with runtime.app_factory() as db:
        replay = await begin(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=req_hash,
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])
    clean_tools = _normalize_tools(tools, allow=(dom is _DOMAINS["experts"]))
    harness_hits = [pair for pair in clean_tools if pair in _HARNESS_TOOL_KEYS]
    if harness_hits:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "VALIDATION_ERROR",
                "message": f"平台工具不可进作者工具集：{harness_hits[0][0]}@{harness_hits[0][1]}",
            },
        )
    async with owner_session(runtime, user_id) as db:
        await _assert_expert_author(db, user_id)
        # D21 锁序：先实体行 FOR UPDATE（与 edit/approve/takedown 同序）——
        # 串行化并发编辑的草稿覆写；否则 autocheck 校验的快照与随后落库的
        # pending_review 行可被并发 PUT 替换（autocheck TOCTOU，D9 提交闸被绕过）
        entity = (
            await db.execute(select(dom.entity).where(dom.entity.id == eid).with_for_update())
        ).scalar_one_or_none()
        if entity is None:
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "实体不存在"}
            )
        revision = (
            await db.execute(
                select(dom.revision).where(
                    dom.revision.id == rid, getattr(dom.revision, dom.fk_field) == eid
                )
            )
        ).scalar_one_or_none()
        if revision is None:
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "revision 不存在"}
            )
        if revision.status != "draft":
            raise AgentCraftError(
                ErrorCode.REVIEW_PENDING, "仅 draft 状态可提交审核", http_status=409
            )
        result = run_auto_check(dom.revision_type, revision.content_json)
        if not result["valid"]:
            summary = "；".join(f"{i['field']}:{i['rule']}" for i in result["issues"][:3])
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "VALIDATION_ERROR",
                    "message": f"自动检查未通过：{summary}",
                },
            )
        if clean_tools:
            rows = (
                await db.execute(
                    select(ToolCatalog.tool_id, ToolCatalog.version).where(
                        tuple_(ToolCatalog.tool_id, ToolCatalog.version).in_(clean_tools)
                    )
                )
            ).all()
            found = {(t, v) for t, v in rows}
            missing = [pair for pair in clean_tools if pair not in found]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "VALIDATION_ERROR",
                        "message": f"工具不在目录中：{missing[0][0]}@{missing[0][1]}",
                    },
                )
            for tool_id, version in clean_tools:
                # 无需先清：tools 写行只发生在 draft→pending_review 的单次提交
                # （重复 submit 在 status 门即 409；覆写草稿走新 revision id）
                db.add(
                    RevisionTool(
                        id=uuid7(),
                        expert_revision_id=revision.id,
                        tool_id=tool_id,
                        version=version,
                    )
                )
            # 先落工具行（此刻父仍 draft，0007 触发器放行），再翻状态——
            # 同 flush 内 UOW 先发父 UPDATE 会让触发器在 INSERT 时点看到
            # 非 draft 父行误 RAISE
            await db.flush()
        revision.status = "pending_review"
        await db.flush()
        # onupdate 时间戳落地后再出参（MissingGreenlet 防护，同 create/edit）
        await db.refresh(revision)
        payload = {"data": {"revision": _revision_brief(revision)}}
        await store(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=req_hash,
            status_code=200,
            response_json=payload,
        )
    return payload["data"]


def _normalize_tools(tools: list[dict], *, allow: bool) -> list[tuple[str, str]]:
    if not allow and tools:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "skill revision 不携带工具"},
        )
    pairs: list[tuple[str, str]] = []
    for item in tools or []:
        tool_id = item.get("tool_id")
        version = item.get("version")
        if (
            not isinstance(tool_id, str)
            or not tool_id
            or not isinstance(version, str)
            or not version
        ):
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": "tools 条目格式非法"},
            )
        pair = (tool_id, version)
        if pair not in pairs:
            pairs.append(pair)
    if len(pairs) > _MAX_TOOLS:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": f"工具最多 {_MAX_TOOLS} 项"},
        )
    return pairs


async def offline_entity(
    runtime: V2Runtime,
    *,
    user_id: str,
    target: str,
    entity_id: str,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """下架（Sup §10.6，Phase 8 D3a）：实体 status published→draft 单向翻转。

    published_revision_id 保留——discover/建任务立即不可见（status 门），历史
    任务快照不受影响；draft 调用=幂等成功（已处目标态，无状态写）；archived
    不复活（409 REVISION_NOT_PUBLISHED，词表冻结不可逆）。同事务审计
    expert.offline/skill.offline（detail=entity_id/kind/previous_status，零他人
    标识；audit_logs INSERT 授权随迁移 0011）。幂等 begin 先于实体查询（§7
    重放先于一切状态门）。
    """
    dom = _domain(target)
    user_id = str(user_id)  # D26 归一
    eid = _reject_invalid_uuid(entity_id, "entity_id")
    route = f"{dom.route_prefix}/{entity_id}/offline"
    async with runtime.app_factory() as db:
        replay = await begin(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])
    async with owner_session(runtime, user_id) as db:
        await _assert_expert_author(db, user_id)
        # 实体行锁（D21 锁序家族一致）：串行化并发编辑/提审/approve CAS
        entity = (
            await db.execute(select(dom.entity).where(dom.entity.id == eid).with_for_update())
        ).scalar_one_or_none()
        if entity is None:  # 跨 owner 或不存在 → 统一 404
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "实体不存在"}
            )
        previous_status = entity.status
        if previous_status == "published":
            entity.status = "draft"  # 0006 guard（0011 修订）仅放行该单向翻转
            await db.flush()
            await db.refresh(entity)  # onupdate 时间戳落地后再出参（同 create/edit）
        elif previous_status == "archived":  # 不复活（目前无写入面，防御词表分支）
            raise AgentCraftError(
                ErrorCode.REVISION_NOT_PUBLISHED, "archived 实体不可下架", http_status=409
            )
        payload = {"data": {"entity": _entity_brief(dom, entity)}}
        db.add(
            AuditLog(
                actor_id=_uuid.UUID(user_id),
                action=f"{dom.entity_type}.offline",
                target_type=dom.entity_type,
                target_id=entity.id,
                reason="author offline",
                detail={
                    "entity_id": str(entity.id),
                    "kind": dom.entity_type,
                    "previous_status": previous_status,
                },
            )
        )
        await store(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=payload,
        )
    return payload["data"]


async def delete_entity(
    runtime: V2Runtime,
    *,
    user_id: str,
    target: str,
    entity_id: str,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """物理删除（Sup §10.6，Phase 8 D3a）：实体+全部 revisions 一并毁灭。

    引用统计（admin 会话权威读）：experts → tasks FK RESTRICT 行数；skills →
    对侧 expert_revisions.skill_refs JSONB 命中的去重 expert 数。计数必须吃
    *_admin_read USING(true)——owner 上下文 RLS 看不见他人 tasks/引用方 draft，
    app 会话计数必漏报。被引用 → 409 ENTITY_IN_USE，文案仅引用计数（零他人
    标识）；计数与删除间竞态由 FK RESTRICT IntegrityError 兜底（同 409）。
    无引用 → 同事务删实体（DB 级联 revisions/revision_tools，0011 depth 放行）
    + 审计 expert.delete/skill.delete（物理毁灭必须有审计痕）。幂等 begin 先于
    实体查询——物理删后同 key 同 body 重放 200 原响应（§7 红线）。
    """
    dom = _domain(target)
    user_id = str(user_id)  # D26 归一
    eid = _reject_invalid_uuid(entity_id, "entity_id")
    route = f"{dom.route_prefix}/{entity_id}"
    async with runtime.app_factory() as db:
        replay = await begin(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])
    async with owner_session(runtime, user_id) as db:
        await _assert_expert_author(db, user_id)
        entity = (
            await db.execute(select(dom.entity).where(dom.entity.id == eid).with_for_update())
        ).scalar_one_or_none()
        if entity is None:  # 跨 owner 或不存在 → 统一 404
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "实体不存在"}
            )
        reference_count = await _count_references(runtime, dom, eid)
        if reference_count > 0:
            message = (
                f"被 {reference_count} 个任务引用"
                if dom is _DOMAINS["experts"]
                else f"被 {reference_count} 个专家引用"
            )
            raise AgentCraftError(ErrorCode.ENTITY_IN_USE, message, http_status=409)
        entity_brief = _entity_brief(dom, entity)  # 删除前取快照（出参/重放载荷）
        try:
            await db.delete(entity)
            await db.flush()
        except IntegrityError as exc:  # 计数后竞态引用（FK RESTRICT 先行拦死级联）
            raise AgentCraftError(
                ErrorCode.ENTITY_IN_USE, "实体正被任务引用，无法删除", http_status=409
            ) from exc
        payload = {"data": {"deleted": True, "entity": entity_brief}}
        db.add(
            AuditLog(
                actor_id=_uuid.UUID(user_id),
                action=f"{dom.entity_type}.delete",
                target_type=dom.entity_type,
                target_id=eid,
                reason="author delete",
                detail={
                    "entity_id": str(eid),
                    "kind": dom.entity_type,
                    "reference_count": reference_count,
                },
            )
        )
        await store(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=payload,
        )
    return payload["data"]


async def _count_references(runtime: V2Runtime, dom: _Domain, entity_id: _uuid.UUID) -> int:
    """引用计数（admin 会话权威读，RLS 全量面）：见 delete_entity docstring。

    skills 侧 JSONB 包含匹配（任一 skill_refs 元素含该 skill_id 即命中）；
    去重按 expert_id 计数（同一 expert 多 revision 引用不重复计）。
    """
    if dom is _DOMAINS["experts"]:
        sql = (
            "SELECT count(*) FROM tasks t "
            "JOIN expert_revisions r ON r.id = t.expert_revision_id "
            "WHERE r.expert_id = CAST(:e AS uuid)"
        )
        params: dict = {"e": str(entity_id)}
    else:
        sql = (
            "SELECT count(DISTINCT r.expert_id) FROM expert_revisions r "
            "WHERE r.content_json->'skill_refs' @> CAST(:needle AS jsonb)"
        )
        params = {"needle": json.dumps([{"skill_id": str(entity_id)}])}
    async with runtime.admin_factory() as db:
        async with db.begin():
            return (await db.execute(text(sql), params)).scalar_one()
