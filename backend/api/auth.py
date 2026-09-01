from fastapi import APIRouter, HTTPException, status

from backend.schemas.user import LoginRequest, RegisterRequest

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/login")
async def login(payload: LoginRequest) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
