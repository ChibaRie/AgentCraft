"""认证接口：注册与登录（Engineering Spec §6.2）。"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.user import User
from backend.schemas.user import AuthResponse, LoginRequest, RegisterRequest
from backend.services import user_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, AuthResponse]:
    user: User = await user_service.register_user(
        db, payload.username, payload.email, payload.password
    )
    # PRD §4.1.2：注册成功即自动登录，直接下发 token
    token = user_service.create_access_token(user.id)
    return {
        "data": AuthResponse(
            id=user.id, username=user.username, email=user.email, role=user.role, token=token
        )
    }


@router.post("/login")
async def login(
    payload: LoginRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, AuthResponse]:
    user: User = await user_service.authenticate_user(db, payload.login, payload.password)
    token = user_service.create_access_token(user.id)
    return {
        "data": AuthResponse(
            id=user.id, username=user.username, email=user.email, role=user.role, token=token
        )
    }
