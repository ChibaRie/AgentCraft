"""V2 作者面路由（Phase 4 裁决 D6/D7；A1 实现期前缀 /api/v2 已随 Phase 8 切契约路径 /api）。

端点：POST/GET /{experts|skills}、GET/PUT/DELETE /{experts|skills}/{id}、
POST /{experts|skills}/{id}/offline、POST /{experts|skills}/{id}/revisions/
{revision_id}/submit（offline/DELETE 属 Phase 8 D3a，Sup §10.6）。
写端点：Idempotency-Key 必带 + CSRF（get_v2_auth）+ 服务层 expert_author 门；
读端点：get_v2_auth + owner_session（owner RLS）。
Replay 分流形态照抄 api/v2/providers.py:63-65（重放不带 Set-Cookie）。
offline/DELETE 无请求体：request_hash(None)（revoke_provider 同款）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from backend.api.v2.content_schemas import (
    ExpertContentPayload,
    SkillContentPayload,
    SubmitPayload,
)
from backend.api.v2.schemas import V2BaseModel  # noqa: F401  # 导出一致性
from backend.v2 import author_service, idempotency
from backend.v2.idempotency import require_key_header
from backend.v2.models.content import Skill, SkillRevision
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, client_ip, get_v2_runtime, owner_session
from backend.v2.session_service import V2AuthContext, get_v2_auth

router = APIRouter()


def _replay_or(outcome) -> JSONResponse:
    if isinstance(outcome, author_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content={"data": outcome})


def _register(domain: str, payload_model, path_prefix: str, tag: str) -> None:
    """experts/skills 同构五端点的注册工厂（避免 10 个手写函数漂移）。"""

    @router.post(path_prefix, tags=[tag])
    async def _create(
        payload: payload_model,  # type: ignore[valid-type]
        user_ctx: V2AuthContext = Depends(get_v2_auth),
        idem_key: str = Depends(require_key_header),
        runtime: V2Runtime = Depends(get_v2_runtime),
    ) -> JSONResponse:
        content = payload.model_dump()
        outcome = await author_service.create_entity(
            runtime,
            user_id=str(user_ctx.user.id),
            target=domain,
            content=content,
            idem_key=idem_key,
            idem_hash=idempotency.request_hash(content),
        )
        return _replay_or(outcome)

    @router.get(path_prefix, tags=[tag])
    async def _list(
        status: str | None = Query(default=None),
        user_ctx: V2AuthContext = Depends(get_v2_auth),
        runtime: V2Runtime = Depends(get_v2_runtime),
    ) -> JSONResponse:
        async with owner_session(runtime, str(user_ctx.user.id)) as db:
            items = await author_service.list_entities(
                db, user_id=str(user_ctx.user.id), target=domain, status=status
            )
        return JSONResponse(status_code=200, content={"data": items})

    @router.get(f"{path_prefix}/{{entity_id}}", tags=[tag])
    async def _detail(
        entity_id: str,
        user_ctx: V2AuthContext = Depends(get_v2_auth),
        runtime: V2Runtime = Depends(get_v2_runtime),
    ) -> JSONResponse:
        async with owner_session(runtime, str(user_ctx.user.id)) as db:
            detail = await author_service.get_entity(
                db, user_id=str(user_ctx.user.id), target=domain, entity_id=entity_id
            )
        return JSONResponse(status_code=200, content={"data": detail})

    @router.put(f"{path_prefix}/{{entity_id}}", tags=[tag])
    async def _edit(
        entity_id: str,
        payload: payload_model,  # type: ignore[valid-type]
        user_ctx: V2AuthContext = Depends(get_v2_auth),
        idem_key: str = Depends(require_key_header),
        runtime: V2Runtime = Depends(get_v2_runtime),
    ) -> JSONResponse:
        content = payload.model_dump()
        outcome = await author_service.edit_entity(
            runtime,
            user_id=str(user_ctx.user.id),
            target=domain,
            entity_id=entity_id,
            content=content,
            idem_key=idem_key,
            idem_hash=idempotency.request_hash(content),
        )
        return _replay_or(outcome)

    @router.post(f"{path_prefix}/{{entity_id}}/revisions/{{revision_id}}/submit", tags=[tag])
    async def _submit(
        entity_id: str,
        revision_id: str,
        payload: SubmitPayload,
        user_ctx: V2AuthContext = Depends(get_v2_auth),
        idem_key: str = Depends(require_key_header),
        runtime: V2Runtime = Depends(get_v2_runtime),
    ) -> JSONResponse:
        body = {"tools": [t.model_dump() for t in payload.tools]}
        outcome = await author_service.submit_revision(
            runtime,
            user_id=str(user_ctx.user.id),
            target=domain,
            entity_id=entity_id,
            revision_id=revision_id,
            tools=body["tools"],
            idem_key=idem_key,
            idem_hash=idempotency.request_hash(body),
        )
        return _replay_or(outcome)

    @router.post(f"{path_prefix}/{{entity_id}}/offline", tags=[tag])
    async def _offline(
        entity_id: str,
        user_ctx: V2AuthContext = Depends(get_v2_auth),
        idem_key: str = Depends(require_key_header),
        runtime: V2Runtime = Depends(get_v2_runtime),
    ) -> JSONResponse:
        """下架（Sup §10.6，D3a）：published→draft，指针保留；draft=幂等成功。"""
        outcome = await author_service.offline_entity(
            runtime,
            user_id=str(user_ctx.user.id),
            target=domain,
            entity_id=entity_id,
            idem_key=idem_key,
            idem_hash=idempotency.request_hash(None),
        )
        return _replay_or(outcome)

    @router.delete(f"{path_prefix}/{{entity_id}}", tags=[tag])
    async def _delete(
        entity_id: str,
        user_ctx: V2AuthContext = Depends(get_v2_auth),
        idem_key: str = Depends(require_key_header),
        runtime: V2Runtime = Depends(get_v2_runtime),
    ) -> JSONResponse:
        """物理删除（Sup §10.6，D3a）：无引用才可删；被引用 409 ENTITY_IN_USE。"""
        outcome = await author_service.delete_entity(
            runtime,
            user_id=str(user_ctx.user.id),
            target=domain,
            entity_id=entity_id,
            idem_key=idem_key,
            idem_hash=idempotency.request_hash(None),
        )
        return _replay_or(outcome)


# ---------------------------------------------------------------------------
# 他人 published skill 公开枚举（Phase 9 T2：Sup §10.11(a)）
# ---------------------------------------------------------------------------
#
# 声明位置即契约：必须在 _register（含 /skills/{entity_id} 动态路由）
# 之前完成声明，否则 /skills/public 会被动态段先匹配。

_SKILL_PUBLIC_PAGE_SIZE_MAX = 50


def _public_skill_card(skill: Skill, revision: SkillRevision) -> dict:
    """非敏感卡五键（Sup §10.11(a)）：不含方法论正文/不含 owner。"""
    content = revision.content_json
    return {
        "id": str(skill.id),
        "published_revision_id": str(skill.published_revision_id),
        "name": content.get("name"),
        "description": content.get("description"),
        "category": content.get("category"),
    }


@router.get("/skills/public", tags=["authoring"])
async def list_public_skills(
    request: Request,
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_SKILL_PUBLIC_PAGE_SIZE_MAX),
    user_ctx: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """已发布 skill 公开枚举（限已登录）：裸 app 会话→ skills/skill_revisions
    published_read policy 仅放行 published 行；status 仅接受 published（缺省
    即 published）。限流沿 discover 口径：60/h/IP。"""
    if status is not None and status != "published":
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "status 仅支持 published"},
        )
    async with runtime.app_factory() as db:
        await enforce(db, scope="discover", subjects=[hmac_subject("ip", client_ip(request))])
        query = (
            select(Skill, SkillRevision)
            .join(SkillRevision, SkillRevision.id == Skill.published_revision_id)
            .where(Skill.status == "published")
        )
        total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        rows = (
            await db.execute(
                query.order_by(Skill.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [_public_skill_card(skill, revision) for skill, revision in rows]
    return JSONResponse(
        status_code=200,
        content={"data": {"items": items, "total": total, "page": page, "page_size": page_size}},
    )


_register("experts", ExpertContentPayload, "/experts", "authoring")
_register("skills", SkillContentPayload, "/skills", "authoring")
