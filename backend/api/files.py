"""任务文件上传/列表接口（Engineering Spec §6.6）。

编排顺序：前置校验（所有权/状态）→ FileService 整批暂存 → 任务累计配额核对
→ 整批落库与移动，任一步失败则补偿删除本批文件。
"""

import logging

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.dependencies import get_file_service
from backend.middleware.auth import get_current_user_id
from backend.services import task_service
from backend.services.file_service import (
    FileCountExceededError,
    FileQuotaExceededError,
    FileService,
    FileTooLargeError,
)

router = APIRouter(prefix="/tasks", tags=["task-files"])

logger = logging.getLogger("agentcraft")


@router.post("/{task_id}/files", status_code=201)
async def upload_files(
    task_id: int,
    files: list[UploadFile] = File(...),
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    service: FileService = Depends(get_file_service),
) -> dict[str, object]:
    await task_service.assert_upload_allowed(db, user_id, task_id)
    staged = []
    try:
        staged = await service.stage_batch(task_id, files)
        service.place_batch(task_id, staged)
        rows = await task_service.commit_task_files(db, service, task_id, staged)
        return {"data": {"files": [task_service.task_file_payload(row) for row in rows]}}
    except Exception as exc:
        if isinstance(exc, (FileTooLargeError, FileCountExceededError, FileQuotaExceededError)):
            # 观测（红线 §4.7）：超限拒绝记 task_id/错误码/文件数，正文与文件名不落日志
            logger.warning(
                "Task %s: 上传超限被拒绝 code=%s 请求数=%d",
                task_id,
                exc.code,
                len(files),
            )
        if staged:
            service.discard(staged)
        raise


@router.get("/{task_id}/files")
async def list_files(
    task_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    task, files, _messages = await task_service.get_task_detail(db, user_id, task_id)
    return {"data": [task_service.task_file_payload(item) for item in files]}
