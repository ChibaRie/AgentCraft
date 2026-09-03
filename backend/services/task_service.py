"""任务与对话业务逻辑（Engineering Spec §6.6、DB 设计 §3.5-§3.8/§5.5、快照机制 §6）。

- 创建：校验 workdir 相对路径与专家 published；冻结 expert_name/avatar、
  skill_snapshot（enabled ∩ published 的完整内容）、mcp_snapshot；建 Task + Conversation
- 状态机：created --首条消息--> running；failed → running（重试）；
  completed 禁止发送（409）；专家下架禁止发送（409）
- 消息持久化时序（§7.6）：用户消息先落库；assistant 完整回复随 final 落库
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.engine.skill_loader import PromptTooLargeError, SkillLoader
from backend.models.conversation import Conversation
from backend.models.expert import Expert
from backend.models.expert_skill import ExpertSkill
from backend.models.message import Message
from backend.models.skill import Skill
from backend.models.task import Task
from backend.models.task_file import TaskFile
from backend.schemas.task import TaskCreateRequest
from backend.services import mcp_service, provider_service
from backend.services.task_locks import task_data_lock
from backend.services.user_service import UserSystemError
from backend.services.workspace import (
    WorkdirInvalidError,
    derive_stored_workdir,
    parse_relative_workdir,
    resolve_workspace_dir,
)


class TaskNotFoundError(UserSystemError):
    status_code = 404
    code = "NOT_FOUND"


class TaskForbiddenError(UserSystemError):
    status_code = 403
    code = "FORBIDDEN"


class TaskStateError(UserSystemError):
    status_code = 409
    code = "INVALID_STATE_TRANSITION"


class ExpertNotAvailableError(UserSystemError):
    status_code = 404
    code = "EXPERT_NOT_AVAILABLE"


class ExpertOfflineError(UserSystemError):
    status_code = 409
    code = "EXPERT_OFFLINE"


class TaskAlreadyStartedError(UserSystemError):
    status_code = 409
    code = "TASK_ALREADY_STARTED"


class TaskPromptTooLargeError(UserSystemError):
    status_code = 413
    code = "PROMPT_TOO_LARGE"


_TITLE_MAX_LENGTH = 200
_WORKDIR_MAX_LENGTH = 500  # tasks.workdir VARCHAR(500)，DB 设计 §3.5
_SKILL_CONTENT_SECTIONS = (
    "role",
    "goal",
    "steps",
    "output_requirements",
    "constraints",
)
_SECTION_LABELS = {
    "role": "角色",
    "goal": "目标",
    "steps": "工作步骤",
    "output_requirements": "输出要求",
    "constraints": "约束",
}


@dataclass(frozen=True)
class SendContext:
    """一次发送的就绪上下文：状态已流转、用户消息已落库。"""

    task_id: int
    conversation_id: int
    content: str
    seq: int
    task: Task


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _skill_content(skill: Skill) -> str:
    """创建时点的 Skill 完整内容（DB 设计 §6.2：content 为完整文本）。"""
    lines = []
    for field_name in _SKILL_CONTENT_SECTIONS:
        value = getattr(skill, field_name, None)
        if value:
            lines.append(f"{_SECTION_LABELS[field_name]}：{value}")
    return "\n".join(lines)


async def _load_enabled_published_skills(db: AsyncSession, expert_id: int) -> list[Skill]:
    """与专家中心公开口径一致：enabled 绑定 ∩ Skill 当前 published。"""
    result = await db.execute(
        select(Skill)
        .join(ExpertSkill, ExpertSkill.skill_id == Skill.id)
        .where(
            ExpertSkill.expert_id == expert_id,
            ExpertSkill.enabled.is_(True),
            Skill.status == "published",
        )
        .order_by(ExpertSkill.id)
    )
    return list(result.scalars())


async def create_task(
    db: AsyncSession,
    user_id: int,
    payload: TaskCreateRequest,
    workspace_root: Path,
    loader: "SkillLoader | None" = None,
    settings: "Settings | None" = None,
) -> tuple[Task, Conversation]:
    relative = parse_relative_workdir(payload.workdir)
    if relative:
        directory = resolve_workspace_dir(Path(workspace_root), relative)
        if not directory.is_dir():
            raise WorkdirInvalidError("工作目录不存在或不可访问")
    stored_workdir = derive_stored_workdir(relative)
    if len(stored_workdir) > _WORKDIR_MAX_LENGTH:
        raise WorkdirInvalidError("工作目录路径过长")

    expert = (
        await db.execute(select(Expert).where(Expert.id == payload.expert_id))
    ).scalar_one_or_none()
    if expert is None or expert.status != "published":
        raise ExpertNotAvailableError("专家不存在或未发布")

    skills = await _load_enabled_published_skills(db, expert.id)
    skill_snapshot = {
        "skills": [{"name": skill.name, "content": _skill_content(skill)} for skill in skills],
        "expert_persona": expert.persona,
        "expert_methodology": expert.methodology,
        "loaded_at": _now().isoformat(),
    }
    # MCP 能力上限快照（§12 决策 15）：enabled 绑定 ∩ published Server ∩
    # enabled 工具 ∩（非敏感 或 已授权）；任务创建后新增工具不进入旧任务
    mcp_tools = await mcp_service.load_snapshot_tools(db, expert.id)
    mcp_snapshot = {"tools": mcp_tools, "loaded_at": _now().isoformat()}

    # 64KiB 上限在创建时把关（§7.5）：超长 prompt 禁止进入后续 argv
    try:
        (loader or SkillLoader()).build_system_prompt(
            skill_snapshot, [], expert_name=expert.name
        )
    except PromptTooLargeError as exc:
        raise TaskPromptTooLargeError(str(exc)) from exc

    # Provider 解析与快照冻结（§7.7：显式 → 用户默认 → 系统默认）
    assert settings is not None
    provider_snapshot, provider_config_id = await provider_service.resolve_task_provider(
        db, user_id, payload.provider_config_id, settings
    )

    task = Task(
        user_id=user_id,
        expert_id=expert.id,
        expert_name_snapshot=expert.name,
        expert_avatar_snapshot=expert.avatar_url,
        title=payload.description[:_TITLE_MAX_LENGTH],
        status="created",
        skill_snapshot=json.dumps(skill_snapshot, ensure_ascii=False),
        mcp_snapshot=json.dumps(mcp_snapshot, ensure_ascii=False),
        provider_config_id=provider_config_id,
        provider_snapshot=json.dumps(provider_snapshot, ensure_ascii=False),
        workdir=stored_workdir,
    )
    conversation = Conversation(task_id=0)
    db.add(task)
    await db.flush()
    conversation.task_id = task.id
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return task, conversation


async def list_tasks(
    db: AsyncSession, user_id: int, page: int, size: int
) -> tuple[list[Task], int]:
    total = (
        await db.execute(select(func.count()).select_from(Task).where(Task.user_id == user_id))
    ).scalar_one()
    result = await db.execute(
        select(Task)
        .where(Task.user_id == user_id)
        .order_by(Task.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    return list(result.scalars()), total


async def _get_owned_task(db: AsyncSession, user_id: int, task_id: int) -> Task:
    task = await db.get(Task, task_id)
    if task is None:
        raise TaskNotFoundError("任务不存在")
    if task.user_id != user_id:
        raise TaskForbiddenError("无权访问该任务")
    return task


async def get_task_detail(
    db: AsyncSession, user_id: int, task_id: int
) -> tuple[Task, list[TaskFile], list[Message]]:
    task = await _get_owned_task(db, user_id, task_id)
    files = list(
        (
            await db.execute(
                select(TaskFile)
                .where(TaskFile.task_id == task_id)
                .order_by(TaskFile.id)
            )
        ).scalars()
    )
    conversation = (
        await db.execute(select(Conversation).where(Conversation.task_id == task_id))
    ).scalar_one_or_none()
    messages: list[Message] = []
    if conversation is not None:
        # created_at 为秒级精度，以 id 作为稳定的次序兜底
        messages = list(
            (
                await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at, Message.id)
                )
            ).scalars()
        )
    return task, files, messages


async def _get_conversation_id(db: AsyncSession, task_id: int) -> int:
    conversation = (
        await db.execute(select(Conversation).where(Conversation.task_id == task_id))
    ).scalar_one_or_none()
    if conversation is None:
        raise TaskNotFoundError("任务会话不存在")
    return conversation.id


async def prepare_send(
    db: AsyncSession, user_id: int, task_id: int, content: str
) -> SendContext:
    """发送前置校验 + 状态流转 + 用户消息落库；全部通过后引擎才开始产流。

    状态流转与用户消息落库在 task data lock 内完成，与上传提交互斥
    （首条消息原子冻结 TaskFile manifest，§6.6）；seq 也在锁内计算，避免并发重复。
    """
    task = await _get_owned_task(db, user_id, task_id)

    expert = await db.get(Expert, task.expert_id)
    if expert is None or expert.status != "published":
        raise ExpertOfflineError("专家已下架，任务不可继续发送")

    if task.status == "completed":
        raise TaskStateError("任务已结束，不可继续发送")

    conversation_id = await _get_conversation_id(db, task_id)

    async with task_data_lock(task_id):
        # created/failed → running 的原子流转；running 直接继续对话
        if task.status in ("created", "failed"):
            previous = task.status
            result = await db.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == previous)
                .values(status="running", updated_at=_now())
            )
            if result.rowcount != 1:
                await db.rollback()
                raise TaskStateError("任务状态已变化，请刷新后重试")

        db.add(Message(conversation_id=conversation_id, role="user", content=content))
        await db.commit()

        seq = (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conversation_id, Message.role == "user")
            )
        ).scalar_one()
    return SendContext(
        task_id=task_id, conversation_id=conversation_id, content=content, seq=seq, task=task
    )


async def persist_assistant_message(
    db: AsyncSession, conversation_id: int, content: str
) -> Message:
    """assistant 完整回复落库（以引擎 final 事件为依据，§7.6）。"""
    message = Message(conversation_id=conversation_id, role="assistant", content=content)
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


async def persist_tool_message(
    db: AsyncSession,
    conversation_id: int,
    tool_call_id: str,
    tool_name: str,
    content: str,
    is_error: bool,
) -> Message:
    """工具执行结果落库（tool_execution_end，§7.6；错误结果同样入库供上下文追溯）。"""
    prefix = f"[tool_error] {content}" if is_error else content
    message = Message(
        conversation_id=conversation_id,
        role="tool",
        content=prefix,
        tool_call_id=tool_call_id[:50] if tool_call_id else None,
        tool_name=tool_name[:50] if tool_name else None,
    )
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


async def fetch_recent_messages(
    session_factory, task_id: int, limit: int
) -> list[dict]:
    """重播种取数（§7.6）：最近 limit 条持久化消息（user/assistant/tool），时间升序。

    使用独立会话：轮处理器在流式生成期间调用，不能复用请求级 session。
    """
    from backend.database import async_session_factory as _factory  # 局部避免循环导入

    factory = session_factory or _factory
    async with factory() as session:
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.task_id == task_id)
            )
        ).scalar_one_or_none()
        if conversation is None:
            return []
        rows = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        rows.reverse()
        return [{"role": item.role, "content": item.content} for item in rows]


async def list_task_file_payloads(db: AsyncSession, task_id: int) -> list[dict]:
    """容器启动所需的 TaskFile manifest（SkillLoader/扩展挂载用）。"""
    rows = list(
        (
            await db.execute(
                select(TaskFile).where(TaskFile.task_id == task_id).order_by(TaskFile.id)
            )
        ).scalars()
    )
    return [task_file_payload(item) for item in rows]


# ---------------------------------------------------------------------------
# 文件上传的 DB 侧（配额核对 + TaskFile 落库）
# ---------------------------------------------------------------------------


async def assert_upload_allowed(db: AsyncSession, user_id: int, task_id: int) -> Task:
    """上传前置：任务存在、属于本人、仍处于 created 且尚无用户消息（§6.6）。"""
    task = await _get_owned_task(db, user_id, task_id)
    if task.status != "created":
        raise TaskAlreadyStartedError("任务已开始，不能再上传文件")
    conversation_id = await _get_conversation_id(db, task_id)
    has_user_message = (
        await db.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id, Message.role == "user")
        )
    ).scalar_one()
    if has_user_message:
        raise TaskAlreadyStartedError("任务已开始，不能再上传文件")
    return task


async def commit_task_files(
    db: AsyncSession, file_service, task_id: int, staged: list[dict]
) -> list[TaskFile]:
    """落库前在 task data lock 内复核任务仍可上传并核对累计配额。

    锁与 prepare_send 共用：上传提交与首条消息流转互斥，关闭并发绕过配额
    与「任务已开始文件仍入库」两个窗口（§6.6 上传并发约束）。
    """
    async with task_data_lock(task_id):
        task = await db.get(Task, task_id)
        if task is None or task.status != "created":
            raise TaskAlreadyStartedError("任务已开始，不能再上传文件")
        conversation_id = await _get_conversation_id(db, task_id)
        has_user_message = (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conversation_id, Message.role == "user")
            )
        ).scalar_one()
        if has_user_message:
            raise TaskAlreadyStartedError("任务已开始，不能再上传文件")

        existing_sum = (
            await db.execute(
                select(func.coalesce(func.sum(TaskFile.size_bytes), 0)).where(
                    TaskFile.task_id == task_id
                )
            )
        ).scalar_one()
        file_service.assert_task_quota(file_service.total_task_bytes(existing_sum, staged))
        try:
            rows = [
                TaskFile(
                    task_id=item["task_id"],
                    original_name=item["original_name"],
                    stored_name=item["stored_name"],
                    relative_path=item["relative_path"],
                    size_bytes=item["size_bytes"],
                    mime_type=item["mime_type"],
                    sha256=item["sha256"],
                )
                for item in staged
            ]
            db.add_all(rows)
            await db.commit()
            return rows
        except Exception:
            await db.rollback()
            raise


def task_file_payload(item: TaskFile) -> dict:
    return {
        "id": item.id,
        "original_name": item.original_name,
        "agent_path": f"/task-files/{item.stored_name}",
        "size_bytes": item.size_bytes,
        "mime_type": item.mime_type,
        "sha256": item.sha256,
        "created_at": item.created_at,
    }
