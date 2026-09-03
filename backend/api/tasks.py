"""任务与对话接口（Engineering Spec §6.6）+ Pi 引擎轮编排（§7.2/§7.6）。

SSE 帧格式：`event: <name>\\ndata: <json>\\n\\n`；事件序 meta →
（queued）→ text_delta×N / tool_event×N → message_saved → done；
事件 schema 严格按 §6.6（EventHandler 翻译）。前置校验失败仍返回统一
JSON 错误信封（非 SSE）。轮锁从发送持有到流结束（§7.2.1）。
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.dependencies import (
    get_pi_engine_manager,
    get_skill_loader,
    get_workspace_root,
)
from backend.engine.pi_engine import PiEngineError
from backend.engine.pi_engine_manager import EngineStateError, PiEngineManager
from backend.engine.skill_loader import PromptTooLargeError, SkillLoader
from backend.middleware.auth import get_current_user_id
from backend.schemas.task import TaskCreateRequest, TaskMessageRequest
from backend.services import provider_service, task_lifecycle, task_service, workspace
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
    loader: SkillLoader = Depends(get_skill_loader),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    task, conversation = await task_service.create_task(
        db, user_id, payload, root, loader, settings
    )
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
            "expert_avatar_snapshot": task.expert_avatar_snapshot,
            "created_at": task.created_at,
            # 快照摘要（创建时冻结，右侧上下文面板展示；§7.5/DB 设计 §6）
            "skills": json.loads(task.skill_snapshot or "{}").get("skills", []),
            "snapshot_loaded_at": json.loads(task.skill_snapshot or "{}").get("loaded_at"),
            "mcp_tools": json.loads(task.mcp_snapshot or "{}").get("tools", []),
            "provider": provider_service.provider_summary(
                json.loads(task.provider_snapshot or "{}")
            ),
            "files": [task_service.task_file_payload(item) for item in files],
            "messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "content": message.content,
                    "tool_name": message.tool_name,
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
    manager: PiEngineManager = Depends(get_pi_engine_manager),
):
    """发送消息并以 SSE 流式返回 Agent 回复（Pi 引擎轮编排，§7.2/§7.6）。

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

    task = context.task
    task_files = await task_service.list_task_file_payloads(db, task_id)
    conversation_id = context.conversation_id

    async def persist_assistant(content: str, usage: dict) -> dict:
        message = await task_service.persist_assistant_message(db, conversation_id, content)
        return {"message_id": message.id, "content": message.content, "usage": usage}

    async def persist_tool(tool_call_id: str, tool_name: str, content: str, is_error: bool) -> dict:
        message = await task_service.persist_tool_message(
            db, conversation_id, tool_call_id, tool_name, content, is_error
        )
        return {"message_id": message.id}

    mcp_tools = json.loads(task.mcp_snapshot or "{}").get("tools", [])
    skill_snapshot = json.loads(task.skill_snapshot or "{}")

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield _sse_frame(
                "meta",
                {"task_id": context.task_id, "seq": context.seq, "started_at": _now_iso()},
            )
            async for name, sse_payload in manager.run_round(
                task_id=task_id,
                user_id=task.user_id,
                provider_config_id=task.provider_config_id,
                stored_workdir=task.workdir,
                skill_snapshot=skill_snapshot,
                task_files=task_files,
                expert_name=task.expert_name_snapshot,
                mcp_tools=mcp_tools,
                content=context.content,
                persist_assistant=persist_assistant,
                persist_tool=persist_tool,
            ):
                yield _sse_frame(name, sse_payload)
        except PromptTooLargeError:
            logger.exception("Task %s prompt too large", task_id)
            yield _sse_frame(
                "error",
                {
                    "code": "PROMPT_TOO_LARGE",
                    "message": "系统提示词超过上限，请精简专家配置",
                    "recoverable": False,
                },
            )
        except (PiEngineError, EngineStateError) as exc:
            logger.exception("Task %s engine failure", task_id)
            yield _sse_frame(
                "error",
                {
                    "code": "ENGINE_ERROR",
                    "message": str(exc) or "引擎异常，请重试",
                    "recoverable": True,
                },
            )
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
    task_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    manager: PiEngineManager = Depends(get_pi_engine_manager),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """结束任务（§7.2.1 结束语义）：预检活动轮→绕锁 abort→等锁→持锁置 completed。"""
    task = await task_lifecycle.complete_task(db, user_id, task_id, manager)
    return {"data": {"task_id": task.id, "status": task.status}}


@router.post("/{task_id}/abort", status_code=status.HTTP_202_ACCEPTED)
async def abort_task(
    task_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    manager: PiEngineManager = Depends(get_pi_engine_manager),
) -> dict[str, object]:
    """用户中止（§7.8/§7.8.1）：仅 running 且存在活动轮可中止（PRD §4.5.6）。

    绕过 mutation lock 直接发 abort，不等待轮结束；任务保持 running（可继续）；
    未完成回复由 EventHandler 按 stopReason=aborted 丢弃不落库。
    """
    task, _files, _messages = await task_service.get_task_detail(db, user_id, task_id)
    if task.status != "running" or not manager.has_active_round(task_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "TASK_NO_ACTIVE_ROUND", "message": "当前没有可中止的 Agent 轮"},
        )
    await manager.request_abort(task_id)
    return {"data": {"task_id": task_id, "abort_requested": True}}


@router.delete("/{task_id}")
async def delete_task(
    task_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    manager: PiEngineManager = Depends(get_pi_engine_manager),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """删除任务（§7.2.1 删除语义）：持锁→停容器→级联删除，项目目录不动。"""
    await task_lifecycle.delete_task(
        db,
        user_id,
        task_id,
        manager,
        task_files_root=Path(settings.HOST_DATA_ROOT) / "task-files",
        extensions_root=Path(settings.HOST_DATA_ROOT) / "extensions",
    )
    return {"data": {"message": "deleted"}}
