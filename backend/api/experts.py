from fastapi import APIRouter, Depends, HTTPException, status

from backend.middleware.auth import get_current_user_id
from backend.schemas.expert import (
    ExpertCreateRequest,
    ExpertMCPBindingRequest,
    ExpertMCPUpdateRequest,
    ExpertSkillBindingRequest,
    ExpertUpdateRequest,
)

router = APIRouter(prefix="/experts", tags=["experts"])
discover_router = APIRouter(prefix="/discover/experts", tags=["discover"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_expert(
    payload: ExpertCreateRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.get("")
async def list_experts(user_id: int = Depends(get_current_user_id)) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.get("/{expert_id}")
async def get_expert(
    expert_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.put("/{expert_id}")
async def update_expert(
    expert_id: int, payload: ExpertUpdateRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{expert_id}/publish")
async def publish_expert(
    expert_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{expert_id}/offline")
async def offline_expert(
    expert_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.delete("/{expert_id}")
async def delete_expert(
    expert_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{expert_id}/skills", status_code=status.HTTP_201_CREATED)
async def bind_skill(
    expert_id: int, payload: ExpertSkillBindingRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.put("/{expert_id}/skills/{skill_id}")
async def update_skill_binding(
    expert_id: int,
    skill_id: int,
    payload: ExpertSkillBindingRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.delete("/{expert_id}/skills/{skill_id}")
async def unbind_skill(
    expert_id: int, skill_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{expert_id}/mcp", status_code=status.HTTP_201_CREATED)
async def bind_mcp(
    expert_id: int, payload: ExpertMCPBindingRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.put("/{expert_id}/mcp/{server_id}")
async def update_mcp_binding(
    expert_id: int,
    server_id: int,
    payload: ExpertMCPUpdateRequest,
    user_id: int = Depends(get_current_user_id),
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.delete("/{expert_id}/mcp/{server_id}")
async def unbind_mcp(
    expert_id: int, server_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@discover_router.get("")
async def list_public_experts() -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@discover_router.get("/{expert_id}")
async def get_public_expert(expert_id: int) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
