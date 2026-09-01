from fastapi import APIRouter, Depends, HTTPException, status

from backend.middleware.auth import get_current_user_id

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def get_me(user_id: int = Depends(get_current_user_id)) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/me/expert")
async def apply_expert(user_id: int = Depends(get_current_user_id)) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
