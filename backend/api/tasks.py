"""任务与对话接口（Engineering Spec §6.6）+ EchoEngine SSE 契约冻结。

SSE 帧格式：`event: <name>\\ndata: <json>\\n\\n`；事件序 meta → text_delta×N →
message_saved → done；前置校验失败仍返回统一 JSON 错误信封（非 SSE）。
complete/abort/delete 随 Pi 引擎阶段实现（§7.2 mutation lock）。
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.dependencies import get_workspace_root
from backend.engine.echo import EchoEngine
from backend.middleware.auth import get_current_user_id
from backend.schemas.task import TaskCreateRequest, TaskMessageRequest
from backend.services import task_service, workspace
from backend.services.task_locks import task_round_lock
from backend.services.workspace import WorkspaceNotFoundError

router = APIRouter(prefix="/tasks", tags=["tasks"])
workspaces_router = APIRouter(tags=["workspaces"])

logger = logging.getLogger("agentcraft")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sse_frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@workspaces_router.get("/workspaces")
async def list_workspaces(
    path: str | None = Query(None),
    user_id: int = Depends(get_current_user_id),
    root: Path = Depends(get_workspace_root),
) -> dict[str, object]:
    """浏览授权根目录内的子目录，供工作目录选择器使用（只列目录）。"""
    relative = workspace.parse_relative_workdir(path)
    directory = workspace.resolve_workspace_dir(root, relative)
    if not directory.is_dir():
        raise WorkspaceNotFoundError("目录不存在")
    directories = sorted(
        (
            {
                "name": item.name,
                "relative_path": f"{relative}/{item.name}" if relative else item.name,
            }
            for item in directory.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        ),
        key=lambda item: item["name"],
    )
    return {
        "data": {
            "root_label": workspace.CANONICAL_AGENT_ROOT,
            "path": relative,
            "directories": directories,
        }
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreateRequest,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    root: Path = Depends(get_workspace_root),
) -> dict[str, object]:
    task, conversation = await task_service.create_task(db, user_id, payload, root)
    return {
        "data": {
            "task_id": task.id,
            "conversation_id": conversation.id,
            "status": task.status,
            "workdir": task.workdir,
        }
    }


@router.get("")
async def list_tasks(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    tasks, total = await task_service.list_tasks(db, user_id, page, size)
    return {
        "data": [
            {
                "id": task.id,
                "title": task.title,
                "status": task.status,
                "expert_name_snapshot": task.expert_name_snapshot,
                "created_at": task.created_at,
            }
            for task in tasks
        ],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/{task_id}")
async def get_task(
    task_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    task, files, messages = await task_service.get_task_detail(db, user_id, task_id)
    return {
        "data": {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "workdir": task.workdir,
            "expert_id": task.expert_id,
            "expert_name_snapshot": task.expert_name_snapshot,
            "created_at": task.created_at,
            "files": [task_service.task_file_payload(item) for item in files],
            "messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "content": message.content,
                    "created_at": message.created_at,
                }
                for message in messages
            ],
        }
    }


@router.post("/{task_id}/messages")
async def send_message(
    task_id: int,
    payload: TaskMessageRequest,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """发送消息并以 SSE 流式返回 Agent 回复（EchoEngine 原样回显）。

    轮锁（round lock）从发送持有到流结束：当前轮未结束时再次发送返回
    429 + Retry-After（§6.6/§7.2.1，Pi 阶段替换为跨进程 mutation lock）。
    """
    round_lock = task_round_lock(task_id)
    if round_lock.locked():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "TASK_ROUND_BUSY", "message": "当前一轮回复仍在进行，请稍后再发送"},
            headers={"Retry-After": "5"},
        )
    await round_lock.acquire()
    try:
        context = await task_service.prepare_send(db, user_id, task_id, payload.content)
    except Exception:
        round_lock.release()
        raise
    engine = EchoEngine()

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield _sse_frame(
                "meta",
                {"task_id": context.task_id, "seq": context.seq, "started_at": _now_iso()},
            )
            async for event in engine.stream(context.content):
                if event.name == "final":
                    message = await task_service.persist_assistant_message(
                        db, context.conversation_id, event.payload["content"]
                    )
                    yield _sse_frame(
                        "message_saved",
                        {"message_id": message.id, "content": message.content},
                    )
                    yield _sse_frame(
                        "done",
                        {"finish_reason": "stop", "usage": event.payload["usage"]},
                    )
                else:
                    yield _sse_frame(event.name, event.payload)
        except Exception:
            # 流已开始，无法改状态码；按 §6.6 以 error 帧告知可重试
            logger.exception("Task %s stream failed", task_id)
            yield _sse_frame(
                "error",
                {"code": "INTERNAL_ERROR", "message": "生成回复失败，请重试", "recoverable": True},
            )
        finally:
            round_lock.release()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
