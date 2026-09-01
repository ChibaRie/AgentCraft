from fastapi import APIRouter, Depends, HTTPException, status

from backend.middleware.auth import get_current_user_id
from backend.schemas.task import TaskCreateRequest, TaskMessageRequest

router = APIRouter(prefix="/tasks", tags=["tasks"])
workspaces_router = APIRouter(tags=["workspaces"])


@workspaces_router.get("/workspaces")
async def list_workspaces(user_id: int = Depends(get_current_user_id)) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreateRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.get("")
async def list_tasks(user_id: int = Depends(get_current_user_id)) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.get("/{task_id}")
async def get_task(task_id: int, user_id: int = Depends(get_current_user_id)) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{task_id}/messages")
async def send_message(
    task_id: int, payload: TaskMessageRequest, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{task_id}/complete")
async def complete_task(
    task_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/{task_id}/abort", status_code=status.HTTP_202_ACCEPTED)
async def abort_task(
    task_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.delete("/{task_id}")
async def delete_task(
    task_id: int, user_id: int = Depends(get_current_user_id)
) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
