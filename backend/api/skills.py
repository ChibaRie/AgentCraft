"""Skill 管理接口（Engineering Spec §6.5）。全部端点要求专家身份。"""

from fastapi import APIRouter, Depends, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.middleware.permission import require_expert_role
from backend.models.user import User
from backend.schemas.skill import (
    BoundExpertRef,
    SkillCreateRequest,
    SkillDeletedResponse,
    SkillDetailResponse,
    SkillResponse,
    SkillUpdateRequest,
    SkillValidateResponse,
    ValidationIssue,
)
from backend.services import skill_import_service, skill_service

router = APIRouter(prefix="/skills", tags=["skills"])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(
    payload: SkillCreateRequest,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, SkillResponse]:
    skill = await skill_service.create_skill(db, user.id, payload)
    return {"data": SkillResponse.model_validate(skill)}


@router.get("")
async def list_skills(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    skills, total = await skill_service.list_skills(db, user.id, page, size)
    return {
        "data": [SkillResponse.model_validate(skill) for skill in skills],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/{skill_id}")
async def get_skill(
    skill_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, SkillDetailResponse]:
    skill, experts = await skill_service.get_skill_detail(db, user.id, skill_id)
    detail = SkillDetailResponse(
        **SkillResponse.model_validate(skill).model_dump(),
        bound_experts=[BoundExpertRef(id=expert.id, name=expert.name) for expert in experts],
    )
    return {"data": detail}


@router.put("/{skill_id}")
async def update_skill(
    skill_id: int,
    payload: SkillUpdateRequest,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, SkillResponse]:
    skill = await skill_service.update_skill(
        db, user.id, skill_id, payload.model_dump(exclude_unset=True)
    )
    return {"data": SkillResponse.model_validate(skill)}


@router.post("/{skill_id}/publish")
async def publish_skill(
    skill_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, SkillResponse]:
    skill = await skill_service.publish_skill(db, user.id, skill_id)
    return {"data": SkillResponse.model_validate(skill)}


@router.post("/{skill_id}/offline")
async def offline_skill(
    skill_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, SkillResponse]:
    skill = await skill_service.offline_skill(db, user.id, skill_id)
    return {"data": SkillResponse.model_validate(skill)}


@router.post("/{skill_id}/validate")
async def validate_skill(
    skill_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, SkillValidateResponse]:
    result = await skill_service.validate_saved_skill(db, user.id, skill_id)
    return {
        "data": SkillValidateResponse(
            valid=result["valid"],
            issues=[ValidationIssue(**issue) for issue in result["issues"]],
        )
    }


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_skill(
    file: UploadFile,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
    settings: "Settings" = Depends(get_settings),
) -> dict[str, object]:
    """导入 Skill（.md → LLM 拆解；.zip → LLM 拆解 + 包落盘），产物为草稿。"""
    from pathlib import Path

    content = await file.read()
    result = await skill_import_service.import_skill_file(
        db,
        user.id,
        filename=file.filename or "",
        content=content,
        settings=settings,
        packages_root=Path(settings.HOST_DATA_ROOT) / "skill-packages",
    )
    return {
        "data": {
            "skill": SkillResponse.model_validate(result["skill"]),
            "files": result["files"],
            "prompt_preview": result["prompt_preview"],
        }
    }


@router.delete("/{skill_id}")
async def delete_skill(
    skill_id: int,
    user: User = Depends(require_expert_role),
    db: AsyncSession = Depends(get_db),
) -> dict[str, SkillDeletedResponse]:
    await skill_service.delete_skill(db, user.id, skill_id)
    return {"data": SkillDeletedResponse(message="deleted")}
