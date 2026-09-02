"""当前用户接口：个人信息与专家身份申请（Engineering Spec §6.2）。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.middleware.auth import get_current_user
from backend.models.user import User
from backend.schemas.user import ExpertApplyResponse, UserMeResponse
from backend.services import user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)) -> dict[str, UserMeResponse]:
    return {
        "data": UserMeResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            role=user.role,
            created_at=user.created_at,
        )
    }


@router.post("/me/expert")
async def apply_expert(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, ExpertApplyResponse]:
    updated: User = await user_service.apply_expert(db, user)
    return {
        "data": ExpertApplyResponse(
            id=updated.id, username=updated.username, email=updated.email, role=updated.role
        )
    }
