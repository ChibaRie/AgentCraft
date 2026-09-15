"""V2 作者面路由（Phase 4 裁决 D6/D7；A1 前缀 /api/v2，Phase 8 切契约路径）。

端点：POST/GET /{experts|skills}、GET/PUT/DELETE /{experts|skills}/{id}、
POST /{experts|skills}/{id}/offline、POST /{experts|skills}/{id}/revisions/
{revision_id}/submit（offline/DELETE 属 Phase 8 D3a，Sup §10.6）。
写端点：Idempotency-Key 必带 + CSRF（get_v2_auth）+ 服务层 expert_author 门；
读端点：get_v2_auth + owner_session（owner RLS）。
Replay 分流形态照抄 api/v2/providers.py:63-65（重放不带 Set-Cookie）。
offline/DELETE 无请求体：request_hash(None)（revoke_provider 同款）。
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from backend.api.v2.content_schemas import (
    ExpertContentPayload,
    SkillContentPayload,
    SubmitPayload,
)
from backend.api.v2.schemas import V2BaseModel  # noqa: F401  # 导出一致性
from backend.v2 import author_service, idempotency
from backend.v2.idempotency import require_key_header
from backend.v2.runtime import V2Runtime, get_v2_runtime, owner_session
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


_register("experts", ExpertContentPayload, "/experts", "authoring")
_register("skills", SkillContentPayload, "/skills", "authoring")
