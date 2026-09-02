"""JWT 认证依赖（Engineering Spec §6.1：除登录/注册外均需 Bearer Token）。"""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.user import User
from backend.services import user_service
from backend.services.user_service import UnauthorizedError

_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "UNAUTHORIZED", "message": "未登录或登录状态已失效"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """校验 Bearer Token 并加载当前用户；任何失败统一 401。"""
    if credentials is None:
        raise _unauthorized()
    try:
        user_id = user_service.decode_access_token(credentials.credentials)
    except UnauthorizedError as exc:
        raise _unauthorized() from exc

    user = await user_service.get_user_by_id(db, user_id)
    if user is None:
        raise _unauthorized()
    return user


async def get_current_user_id(user: User = Depends(get_current_user)) -> int:
    """占位端点沿用的用户 id 依赖；业务实现时可切换为 get_current_user。"""
    return user.id
