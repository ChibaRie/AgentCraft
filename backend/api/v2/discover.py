"""V2 匿名 discover 路由（Sup §5:120「数据源改 published_revision」；D19）。

匿名形态：app_factory 裸会话、不设 owner GUC → experts/skills SELECT policy 仅
放行 status='published' 行（0001:1075-1079 + 行为测试 test_v2_rls.py:375-402）；
revision 经 published_read 可见。无 get_v2_auth（无 cookie 即 401，不可用）；
GET 豁免 CSRF/幂等；限流 discover [ip]（60/h，D12）。

Sup §10.2（Phase 8 T2）：列表项与详情均暴露 published_revision_id——前端召唤
专家直用作 POST /tasks 的 expert_revision_id，不再二次解析。WHERE 实体
status='published'（或裸会话 RLS 等效过滤）→ 字段恒为 UUID 非空。
"""

import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from backend.v2.author_service import _reject_invalid_uuid
from backend.v2.models.content import (
    Expert,
    ExpertRevision,
    RevisionTool,
    SkillRevision,
)
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, client_ip, get_v2_runtime

router = APIRouter()

_AI_GENERATED_NOTICE = "本内容由 AI 辅助生成，经平台审核后发布。"
_SEARCH_MAX = 100
_PAGE_SIZE_MAX = 50


@router.get("/discover/experts")
async def list_discover_experts(
    request: Request,
    search: str | None = Query(default=None, max_length=_SEARCH_MAX),
    category: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_PAGE_SIZE_MAX),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    async with runtime.app_factory() as db:
        await enforce(db, scope="discover", subjects=[hmac_subject("ip", client_ip(request))])
        query = (
            select(Expert, ExpertRevision)
            .join(ExpertRevision, ExpertRevision.id == Expert.published_revision_id)
            .where(Expert.status == "published")
        )
        if category is not None:
            query = query.where(ExpertRevision.content_json["category"].astext == category)
        if search:
            # V1 expert_service.py:321-324 同款三连转义——re.escape 不处理 LIKE
            # 通配符（%/_ 非正则元字符），必须显式转义防 search="%" 全表命中
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            query = query.where(
                ExpertRevision.content_json["name"].astext.ilike(pattern, escape="\\")
                | ExpertRevision.content_json["description"].astext.ilike(pattern, escape="\\")
            )
        total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        rows = (
            await db.execute(
                query.order_by(Expert.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [_card(expert, revision) for expert, revision in rows]
    return JSONResponse(
        status_code=200,
        content={"data": {"items": items, "total": total, "page": page, "page_size": page_size}},
    )


@router.get("/discover/experts/{expert_id}")
async def discover_expert_detail(
    request: Request,
    expert_id: str,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    eid = _reject_invalid_uuid(expert_id, "expert_id")
    async with runtime.app_factory() as db:  # 无 GUC：RLS 仅放行 published
        # D12：详情端点同桶限流（重载荷端点——1+20 次解析查询，不得豁免）
        await enforce(db, scope="discover", subjects=[hmac_subject("ip", client_ip(request))])
        row = (
            await db.execute(
                select(Expert, ExpertRevision)
                .join(ExpertRevision, ExpertRevision.id == Expert.published_revision_id)
                .where(Expert.id == eid)
            )
        ).first()
        if row is None:  # 跨 owner/draft/不存在 → 统一 404（RLS 过滤即不可见）
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "专家不存在"}
            )
        expert, revision = row
        tools = (
            await db.execute(
                select(RevisionTool.tool_id, RevisionTool.version).where(
                    RevisionTool.expert_revision_id == revision.id
                )
            )
        ).all()
        skills: list[dict] = []
        for ref in revision.content_json.get("skill_refs") or []:
            try:
                rid = _uuid.UUID(ref["revision_id"])
            except (ValueError, TypeError):
                continue
            skill_row = (
                await db.execute(
                    select(
                        SkillRevision.skill_id,
                        SkillRevision.revision_no,
                        SkillRevision.content_json,
                    ).where(SkillRevision.id == rid)
                )
            ).first()
            if skill_row is None:
                continue  # 引用已不可见（被 takedown 等）→ 静默剔除（D19）
            skills.append(
                {
                    "skill_id": str(skill_row.skill_id),
                    "name": skill_row.content_json.get("name"),
                    "revision_no": skill_row.revision_no,
                }
            )
    content = revision.content_json
    return JSONResponse(
        status_code=200,
        content={
            "data": {
                "id": str(expert.id),
                "published_revision_id": str(expert.published_revision_id),
                "name": content.get("name"),
                "description": content.get("description"),
                "avatar_url": content.get("avatar_url"),
                "category": content.get("category"),
                "persona": content.get("persona"),
                "methodology": content.get("methodology"),
                "task_examples": content.get("task_examples") or [],
                "skills": skills,
                "tools": [{"tool_id": t, "version": v} for t, v in tools],
                "ai_generated_notice": _AI_GENERATED_NOTICE,
            }
        },
    )


def _card(expert, revision) -> dict:
    content = revision.content_json
    return {
        "id": str(expert.id),
        "published_revision_id": str(expert.published_revision_id),
        "name": content.get("name"),
        "description": content.get("description"),
        "avatar_url": content.get("avatar_url"),
        "category": content.get("category"),
        "skill_count": len(content.get("skill_refs") or []),
    }
