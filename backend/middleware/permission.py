"""权限校验依赖（PRD §2.2 权限矩阵：专家操作要求 role=expert）。"""

from fastapi import Depends, HTTPException, status

from backend.middleware.auth import get_current_user
from backend.models.user import User


def require_expert_role(user: User = Depends(get_current_user)) -> User:
    if user.role != "expert":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "需要专家身份"},
        )
    return user
