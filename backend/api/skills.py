from fastapi import APIRouter, Depends, HTTPException, status

from backend.middleware.auth import get_current_user_id
from backend.schemas.skill import SkillCreateRequest, SkillUpdateRequest

router = APIRouter(prefix="/skills", tags=["skills"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(
    payload: SkillCreateRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.get("")
async def list_skills(user_id: int = Depends(get_current_user_id)) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.get("/{skill_id}")
async def get_skill(
    skill_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.put("/{skill_id}")
async def update_skill(
    skill_id: int, payload: SkillUpdateRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{skill_id}/publish")
async def publish_skill(
    skill_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{skill_id}/offline")
async def offline_skill(
    skill_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{skill_id}/validate")
async def validate_skill(
    skill_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
