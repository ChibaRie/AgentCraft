"""专家管理接口（Engineering Spec §6.3）与专家中心公开接口（§6.4）。

管理面全部要求专家身份；发现面（discover_router）匿名可访问，仅暴露 published 专家。
"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.dependencies import get_pi_engine_manager
from backend.engine.pi_engine_manager import PiEngineManager
from backend.middleware.permission import require_expert_role
from backend.models.user import User
from backend.schemas.expert import (
    BoundSkillRef,
    DeletedResponse,
    DiscoverExpertCard,
    DiscoverExpertDetail,
    DiscoverSkillRef,
    ExpertBindingResponse,
    ExpertBindingToggleRequest,
    ExpertCreateRequest,
    ExpertDetailResponse,
    ExpertMCPBindingRequest,
    ExpertMCPUpdateRequest,
    ExpertResponse,
    ExpertSkillBindingRequest,
    ExpertUpdateRequest,
    UnboundResponse,
)
from backend.services import expert_service, mcp_service, task_lifecycle

router = APIRouter(prefix="/experts", tags=["experts"])
discover_router = APIRouter(prefix="/discover/experts", tags=["discover"])


def _expert_response(expert) -> ExpertResponse:
    # task_examples 在库中是 JSON 文本，先解析再构造响应
    return ExpertResponse(
        id=expert.id,
        name=expert.name,
        description=expert.description,
        avatar_url=expert.avatar_url,
        category=expert.category,
        persona=expert.persona,
        methodology=expert.methodology,
        task_examples=expert_service.parse_task_examples(expert),
        status=expert.status,
        created_at=expert.created_at,
        updated_at=expert.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_expert(
    payload: ExpertCreateRequest,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, ExpertResponse]:
    expert = await expert_service.create_expert(db, user.id, payload)
    return {"data": _expert_response(expert)}


@router.get("")
async def list_experts(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    statuses = None
    if status_filter:
        statuses = [item.strip() for item in status_filter.split(",") if item.strip()]
    experts, total = await expert_service.list_experts(db, user.id, page, size, statuses)
    return {
        "data": [_expert_response(expert) for expert in experts],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/{expert_id}")
async def get_expert(
    expert_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, ExpertDetailResponse]:
    expert, bindings = await expert_service.get_expert_detail(db, user.id, expert_id)
    detail = ExpertDetailResponse(
        **_expert_response(expert).model_dump(),
        skills=[
            BoundSkillRef(
                id=skill.id,
                name=skill.name,
                description=skill.description,
                status=skill.status,
                enabled=binding.enabled,
            )
            for binding, skill in bindings
        ],
        # MCP 绑定列表（server id/name/status/enabled）；连接信息不在此返回（§6.3）
        mcps=await mcp_service.list_expert_bindings(db, user.id, expert_id),
    )
    return {"data": detail}


@router.put("/{expert_id}")
async def update_expert(
    expert_id: int,
    payload: ExpertUpdateRequest,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, ExpertResponse]:
    expert = await expert_service.update_expert(
        db, user.id, expert_id, payload.model_dump(exclude_unset=True)
    )
    return {"data": _expert_response(expert)}


@router.post("/{expert_id}/publish")
async def publish_expert(
    expert_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, ExpertResponse]:
    expert = await expert_service.publish_expert(db, user.id, expert_id)
    return {"data": _expert_response(expert)}


@router.post("/{expert_id}/offline")
async def offline_expert(
    expert_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
    manager: PiEngineManager = Depends(get_pi_engine_manager),
) -> dict[str, ExpertResponse]:
    expert = await expert_service.offline_expert(db, user.id, expert_id)
    # §7.8：该专家 running 任务原子置 completed 并回收容器
    await task_lifecycle.complete_expert_running_tasks(db, expert_id, manager)
    return {"data": _expert_response(expert)}


@router.delete("/{expert_id}")
async def delete_expert(
    expert_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, DeletedResponse]:
    await expert_service.delete_expert(db, user.id, expert_id)
    return {"data": DeletedResponse(message="deleted")}


@router.post("/{expert_id}/skills", status_code=status.HTTP_201_CREATED)
async def bind_skill(
    expert_id: int,
    payload: ExpertSkillBindingRequest,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, ExpertBindingResponse]:
    _expert, _skill, enabled = await expert_service.bind_skill(
        db, user.id, expert_id, payload.skill_id, payload.enabled
    )
    return {
        "data": ExpertBindingResponse(
            expert_id=expert_id, skill_id=payload.skill_id, enabled=enabled
        )
    }


@router.put("/{expert_id}/skills/{skill_id}")
async def update_skill_binding(
    expert_id: int,
    skill_id: int,
    payload: ExpertBindingToggleRequest,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, ExpertBindingResponse]:
    _expert, _skill, enabled = await expert_service.update_skill_binding(
        db, user.id, expert_id, skill_id, payload.enabled
    )
    return {"data": ExpertBindingResponse(expert_id=expert_id, skill_id=skill_id, enabled=enabled)}


@router.delete("/{expert_id}/skills/{skill_id}")
async def unbind_skill(
    expert_id: int,
    skill_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, UnboundResponse]:
    await expert_service.unbind_skill(db, user.id, expert_id, skill_id)
    return {"data": UnboundResponse(message="unbound")}


@router.post("/{expert_id}/mcp", status_code=status.HTTP_201_CREATED)
async def bind_mcp(
    expert_id: int,
    payload: ExpertMCPBindingRequest,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    binding = await mcp_service.bind_server(
        db, user.id, expert_id, payload.server_id, enabled=payload.enabled
    )
    return {
        "data": {
            "expert_id": binding.expert_id,
            "server_id": binding.server_id,
            "enabled": bool(binding.enabled),
        }
    }


@router.put("/{expert_id}/mcp/{server_id}")
async def update_mcp_binding(
    expert_id: int,
    server_id: int,
    payload: ExpertMCPUpdateRequest,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    binding = await mcp_service.update_binding(
        db, user.id, expert_id, server_id, enabled=payload.enabled
    )
    return {
        "data": {
            "expert_id": binding.expert_id,
            "server_id": binding.server_id,
            "enabled": bool(binding.enabled),
        }
    }


@router.delete("/{expert_id}/mcp/{server_id}")
async def unbind_mcp(
    expert_id: int,
    server_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    await mcp_service.unbind_server(db, user.id, expert_id, server_id)
    return {"data": UnboundResponse(message="unbound")}


# ---------------------------------------------------------------------------
# 专家中心（§6.4，匿名可访问）
# ---------------------------------------------------------------------------


@discover_router.get("")
async def list_public_experts(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None, max_length=100),
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    experts, total = await expert_service.discover_experts(db, page, size, search, category)
    return {
        "data": [
            DiscoverExpertCard(
                id=expert.id,
                name=expert.name,
                description=expert.description,
                avatar_url=expert.avatar_url,
                category=expert.category,
                skill_count=count,
            )
            for expert, count in experts
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@discover_router.get("/{expert_id}")
async def get_public_expert(
    expert_id: int, db: AsyncSession = Depends(get_db)
) -> dict[str, DiscoverExpertDetail]:
    expert, skills = await expert_service.discover_expert_detail(db, expert_id)
    detail = DiscoverExpertDetail(
        id=expert.id,
        name=expert.name,
        description=expert.description,
        avatar_url=expert.avatar_url,
        category=expert.category,
        persona=expert.persona,
        methodology=expert.methodology,
        task_examples=expert_service.parse_task_examples(expert),
        skills=[
            DiscoverSkillRef(id=skill.id, name=skill.name, description=skill.description)
            for skill in skills
        ],
    )
    return {"data": detail}
