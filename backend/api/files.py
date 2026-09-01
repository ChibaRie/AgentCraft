from fastapi import APIRouter, Depends, HTTPException, UploadFile, status

from backend.middleware.auth import get_current_user_id

router = APIRouter(prefix="/tasks", tags=["task-files"])


@router.post("/{task_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_files(
    task_id: int, files: list[UploadFile], user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.get("/{task_id}/files")
async def list_files(
    task_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
