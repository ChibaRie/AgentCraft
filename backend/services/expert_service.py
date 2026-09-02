"""专家管理 + 专家中心业务逻辑（Engineering Spec §6.3/§6.4、PRD §4.2、DB 设计 §5.2）。

状态机：draft → published → offline → published；非法流转 409。
发布条件：至少绑定一个 published 且 enabled 的 Skill（§4.2.4 + §6.3）。
删除前置：无任何状态任务引用（§4.2.6，快照规则）。
绑定规则：Skill 必须 published；绑定默认 enabled=false；enabled=true 需内容通过校验。
"""

import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.expert import Expert
from backend.models.expert_skill import ExpertSkill
from backend.models.skill import Skill
from backend.models.task import Task
from backend.schemas.expert import ExpertCreateRequest
from backend.services.user_service import UserSystemError
from harness.mcp.validate_skill import validate_skill


class ExpertNotFoundError(UserSystemError):
    status_code = 404
    code = "NOT_FOUND"


class ExpertForbiddenError(UserSystemError):
    status_code = 403
    code = "FORBIDDEN"


class ExpertPublishConditionError(UserSystemError):
    status_code = 400
    code = "EXPERT_PUBLISH_CONDITION"


class ExpertStateError(UserSystemError):
    status_code = 409
    code = "INVALID_STATE_TRANSITION"


class ExpertStillReferencedError(UserSystemError):
    status_code = 409
    code = "EXPERT_STILL_REFERENCED"


class SkillNotPublishedError(UserSystemError):
    status_code = 400
    code = "SKILL_NOT_PUBLISHED"


class SkillAlreadyBoundError(UserSystemError):
    status_code = 409
    code = "SKILL_ALREADY_BOUND"


class BindingNotFoundError(UserSystemError):
    status_code = 404
    code = "BINDING_NOT_FOUND"


class SkillInvalidError(UserSystemError):
    status_code = 400
    code = "SKILL_INVALID"


_SKILL_CONTENT_FIELDS = (
    "name",
    "description",
    "use_case",
    "role",
    "goal",
    "steps",
    "input_requirements",
    "output_requirements",
    "constraints",
)


def _now():
    return datetime.now(timezone.utc)


def parse_task_examples(expert: Expert) -> list[str] | None:
    if expert.task_examples is None:
        return None
    try:
        value = json.loads(expert.task_examples)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def _skill_content_dict(skill: Skill) -> dict:
    return {field: getattr(skill, field) for field in _SKILL_CONTENT_FIELDS}


async def _get_owned_expert(db: AsyncSession, owner_id: int, expert_id: int) -> Expert:
    expert = await db.get(Expert, expert_id)
    if expert is None:
        raise ExpertNotFoundError("专家不存在")
    if expert.owner_id != owner_id:
        raise ExpertForbiddenError("无权访问该专家")
    return expert


async def _get_binding(db: AsyncSession, expert_id: int, skill_id: int) -> ExpertSkill | None:
    return await db.scalar(
        select(ExpertSkill).where(
            ExpertSkill.expert_id == expert_id, ExpertSkill.skill_id == skill_id
        )
    )


async def _get_owned_skill_or_raise(db: AsyncSession, expert: Expert, skill_id: int) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise ExpertNotFoundError("Skill 不存在")
    if skill.owner_id != expert.owner_id:
        raise ExpertForbiddenError("Skill 属于其他用户")
    return skill


async def create_expert(db: AsyncSession, owner_id: int, payload: ExpertCreateRequest) -> Expert:
    data = payload.model_dump()
    task_examples = data.pop("task_examples")
    expert = Expert(
        owner_id=owner_id,
        status="draft",
        task_examples=json.dumps(task_examples, ensure_ascii=False) if task_examples else None,
        **data,
    )
    db.add(expert)
    await db.commit()
    await db.refresh(expert)
    return expert


async def list_experts(
    db: AsyncSession, owner_id: int, page: int, size: int, statuses: list[str] | None = None
) -> tuple[list[Expert], int]:
    condition = Expert.owner_id == owner_id
    if statuses:
        condition = condition & Expert.status.in_(statuses)
    total = await db.scalar(select(func.count()).select_from(Expert).where(condition))
    statement = (
        select(Expert)
        .where(condition)
        .order_by(Expert.created_at.desc(), Expert.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    experts = list((await db.scalars(statement)).all())
    return experts, int(total or 0)


async def get_expert_detail(
    db: AsyncSession, owner_id: int, expert_id: int
) -> tuple[Expert, list[tuple[ExpertSkill, Skill]]]:
    expert = await _get_owned_expert(db, owner_id, expert_id)
    rows = (
        await db.execute(
            select(ExpertSkill, Skill)
            .join(Skill, Skill.id == ExpertSkill.skill_id)
            .where(ExpertSkill.expert_id == expert_id)
            .order_by(ExpertSkill.created_at, ExpertSkill.id)
        )
    ).all()
    return expert, list(rows)


async def update_expert(db: AsyncSession, owner_id: int, expert_id: int, changes: dict) -> Expert:
    expert = await _get_owned_expert(db, owner_id, expert_id)
    # 先记录键是否出现：显式传 null 表示清空，键缺席表示不修改
    has_examples = "task_examples" in changes
    task_examples = changes.pop("task_examples", None)
    for field, value in changes.items():
        setattr(expert, field, value)
    if has_examples:
        expert.task_examples = (
            json.dumps(task_examples, ensure_ascii=False) if task_examples else None
        )
    expert.updated_at = _now()
    await db.commit()
    await db.refresh(expert)
    return expert


async def publish_expert(db: AsyncSession, owner_id: int, expert_id: int) -> Expert:
    expert = await _get_owned_expert(db, owner_id, expert_id)
    if expert.status not in ("draft", "offline"):
        raise ExpertStateError("当前状态不允许发布")
    enabled_published = await db.scalar(
        select(func.count())
        .select_from(ExpertSkill)
        .join(Skill, Skill.id == ExpertSkill.skill_id)
        .where(
            ExpertSkill.expert_id == expert_id,
            ExpertSkill.enabled.is_(True),
            Skill.status == "published",
        )
    )
    if int(enabled_published or 0) < 1:
        raise ExpertPublishConditionError("发布条件不满足：需至少绑定一个已发布且已启用的 Skill")
    expert.status = "published"
    expert.updated_at = _now()
    await db.commit()
    await db.refresh(expert)
    return expert


async def offline_expert(db: AsyncSession, owner_id: int, expert_id: int) -> Expert:
    expert = await _get_owned_expert(db, owner_id, expert_id)
    if expert.status != "published":
        raise ExpertStateError("只有已发布的专家可以下架")
    # §6.3 下架处理：running 引用任务置 completed 并回收 Pi 容器 —— 任务运行时
    # 在任务阶段接入；TaskService 侧会按 expert.status 拦截后续消息发送
    expert.status = "offline"
    expert.updated_at = _now()
    await db.commit()
    await db.refresh(expert)
    return expert


async def delete_expert(db: AsyncSession, owner_id: int, expert_id: int) -> None:
    expert = await _get_owned_expert(db, owner_id, expert_id)
    task_count = await db.scalar(
        select(func.count()).select_from(Task).where(Task.expert_id == expert_id)
    )
    if int(task_count or 0) > 0:
        raise ExpertStillReferencedError("专家下存在任务引用，无法删除；请先清理该专家下的任务")
    await db.delete(expert)
    await db.commit()


async def bind_skill(
    db: AsyncSession, owner_id: int, expert_id: int, skill_id: int, enabled: bool
) -> tuple[Expert, Skill, bool]:
    expert = await _get_owned_expert(db, owner_id, expert_id)
    skill = await _get_owned_skill_or_raise(db, expert, skill_id)
    if skill.status != "published":
        raise SkillNotPublishedError("只有已发布的 Skill 可以绑定")
    if await _get_binding(db, expert_id, skill_id) is not None:
        raise SkillAlreadyBoundError("该 Skill 已绑定到此专家")
    if enabled:
        result = validate_skill(_skill_content_dict(skill))
        if not result["valid"]:
            raise SkillInvalidError("Skill 内容未通过校验，无法启用绑定")

    db.add(ExpertSkill(expert_id=expert_id, skill_id=skill_id, enabled=enabled))
    await db.commit()
    return expert, skill, enabled


async def update_skill_binding(
    db: AsyncSession, owner_id: int, expert_id: int, skill_id: int, enabled: bool
) -> tuple[Expert, Skill, bool]:
    expert = await _get_owned_expert(db, owner_id, expert_id)
    # 先查绑定（规格错误优先级：404 绑定不存在 → 403 Skill 属于其他用户）
    binding = await _get_binding(db, expert_id, skill_id)
    if binding is None:
        raise BindingNotFoundError("绑定关系不存在")
    skill = await _get_owned_skill_or_raise(db, expert, skill_id)

    if enabled:
        if skill.status != "published":
            raise SkillNotPublishedError("只有已发布的 Skill 可以启用绑定")
        result = validate_skill(_skill_content_dict(skill))
        if not result["valid"]:
            raise SkillInvalidError("Skill 内容未通过校验，无法启用绑定")

    binding.enabled = enabled
    await db.commit()
    return expert, skill, enabled


async def unbind_skill(db: AsyncSession, owner_id: int, expert_id: int, skill_id: int) -> None:
    expert = await _get_owned_expert(db, owner_id, expert_id)
    binding = await _get_binding(db, expert_id, skill_id)
    if binding is None:
        raise BindingNotFoundError("绑定关系不存在")
    await _get_owned_skill_or_raise(db, expert, skill_id)
    await db.delete(binding)
    await db.commit()


# ---------------------------------------------------------------------------
# 专家中心（公开面，仅 published）
# ---------------------------------------------------------------------------


def _public_enabled_skill_condition():
    """公开面的 Skill 口径：绑定启用且当前仍为 published。

    离线 Skill 是运行时 kill switch（手册 §7.5），不应作为公开能力出现或计数，
    同时保证卡片 skill_count 与详情 skills 列表一致。
    """
    return (ExpertSkill.enabled.is_(True)) & (Skill.status == "published")


def _enabled_skill_count_subquery():
    return (
        select(ExpertSkill.expert_id, func.count().label("cnt"))
        .join(Skill, Skill.id == ExpertSkill.skill_id)
        .where(_public_enabled_skill_condition())
        .group_by(ExpertSkill.expert_id)
        .subquery()
    )


async def discover_experts(
    db: AsyncSession, page: int, size: int, search: str | None, category: str | None
) -> tuple[list[tuple[Expert, int]], int]:
    condition = Expert.status == "published"
    if category:
        condition = condition & (Expert.category == category)
    if search:
        # 转义 LIKE 通配符，避免用户输入中的 % _ 被当作模式
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        keyword = f"%{escaped}%"
        condition = condition & (
            Expert.name.like(keyword, escape="\\") | Expert.description.like(keyword, escape="\\")
        )

    counts = _enabled_skill_count_subquery()
    total = await db.scalar(select(func.count()).select_from(Expert).where(condition))
    rows = (
        await db.execute(
            select(Expert, func.coalesce(counts.c.cnt, 0))
            .outerjoin(counts, counts.c.expert_id == Expert.id)
            .where(condition)
            .order_by(Expert.updated_at.desc(), Expert.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    return [(expert, int(count or 0)) for expert, count in rows], int(total or 0)


async def discover_expert_detail(db: AsyncSession, expert_id: int) -> tuple[Expert, list[Skill]]:
    expert = await db.get(Expert, expert_id)
    if expert is None or expert.status != "published":
        raise ExpertNotFoundError("专家不存在或未公开")
    # 仅展示 enabled 绑定且当前仍为 published 的 Skill（离线 Skill 是运行时
    # kill switch，不应继续作为公开能力展示）
    skills = list(
        (
            await db.scalars(
                select(Skill)
                .join(ExpertSkill, ExpertSkill.skill_id == Skill.id)
                .where(
                    ExpertSkill.expert_id == expert_id,
                    _public_enabled_skill_condition(),
                )
                .order_by(ExpertSkill.created_at, ExpertSkill.id)
            )
        ).all()
    )
    return expert, skills
