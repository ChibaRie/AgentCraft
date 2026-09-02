"""Skill 管理业务逻辑（Engineering Spec §6.5、PRD §4.4、DB 设计 §5.3 状态机）。

状态机：draft → published → offline → published；非法流转抛 409。
published 内容编辑必须在保存事务中通过 validate_skill，失败回滚保留原内容。
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.expert import Expert
from backend.models.expert_skill import ExpertSkill
from backend.models.skill import Skill
from backend.schemas.skill import SkillCreateRequest
from backend.services.user_service import UserSystemError
from harness.mcp.validate_skill import validate_skill

_CONTENT_FIELDS = (
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


# UserSystemError 是统一错误信封的基类（{error:{code,message}}），跨服务复用
class SkillNotFoundError(UserSystemError):
    status_code = 404
    code = "NOT_FOUND"


class SkillForbiddenError(UserSystemError):
    status_code = 403
    code = "FORBIDDEN"


class SkillInvalidError(UserSystemError):
    status_code = 400
    code = "SKILL_INVALID"


class SkillStateError(UserSystemError):
    status_code = 409
    code = "INVALID_STATE_TRANSITION"


class SkillBoundError(UserSystemError):
    status_code = 409
    code = "SKILL_STILL_BOUND"


def _now():
    return datetime.now(timezone.utc)


def _content_dict(skill: Skill) -> dict[str, str | None]:
    return {field: getattr(skill, field) for field in _CONTENT_FIELDS}


def _invalid_summary(result: dict) -> str:
    parts = [f"{issue['field']}: {issue['message']}" for issue in result["issues"]]
    return "Skill 校验未通过：" + "；".join(parts[:5])


async def _get_owned_skill(db: AsyncSession, owner_id: int, skill_id: int) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise SkillNotFoundError("Skill 不存在")
    if skill.owner_id != owner_id:
        raise SkillForbiddenError("无权访问该 Skill")
    return skill


async def create_skill(db: AsyncSession, owner_id: int, payload: SkillCreateRequest) -> Skill:
    skill = Skill(owner_id=owner_id, status="draft", **payload.model_dump())
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


async def list_skills(
    db: AsyncSession, owner_id: int, page: int, size: int
) -> tuple[list[Skill], int]:
    total = await db.scalar(
        select(func.count()).select_from(Skill).where(Skill.owner_id == owner_id)
    )
    statement = (
        select(Skill)
        .where(Skill.owner_id == owner_id)
        .order_by(Skill.created_at.desc(), Skill.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    skills = list((await db.scalars(statement)).all())
    return skills, int(total or 0)


async def get_skill_detail(
    db: AsyncSession, owner_id: int, skill_id: int
) -> tuple[Skill, list[Expert]]:
    skill = await _get_owned_skill(db, owner_id, skill_id)
    experts = list(
        (
            await db.scalars(
                select(Expert)
                .join(ExpertSkill, ExpertSkill.expert_id == Expert.id)
                .where(ExpertSkill.skill_id == skill_id)
                .order_by(Expert.id)
            )
        ).all()
    )
    return skill, experts


async def update_skill(db: AsyncSession, owner_id: int, skill_id: int, changes: dict) -> Skill:
    skill = await _get_owned_skill(db, owner_id, skill_id)
    for field, value in changes.items():
        setattr(skill, field, value)

    if skill.status == "published":
        # 规格要求：published 更新在同一保存事务中校验，失败保留原已发布内容
        result = validate_skill(_content_dict(skill))
        if not result["valid"]:
            await db.rollback()
            raise SkillInvalidError(_invalid_summary(result))

    skill.updated_at = _now()
    await db.commit()
    await db.refresh(skill)
    return skill


async def publish_skill(db: AsyncSession, owner_id: int, skill_id: int) -> Skill:
    skill = await _get_owned_skill(db, owner_id, skill_id)
    if skill.status not in ("draft", "offline"):
        raise SkillStateError("当前状态不允许发布")
    result = validate_skill(_content_dict(skill))
    if not result["valid"]:
        raise SkillInvalidError(_invalid_summary(result))
    skill.status = "published"
    skill.updated_at = _now()
    await db.commit()
    await db.refresh(skill)
    return skill


async def offline_skill(db: AsyncSession, owner_id: int, skill_id: int) -> Skill:
    skill = await _get_owned_skill(db, owner_id, skill_id)
    if skill.status != "published":
        raise SkillStateError("只有已发布的 Skill 可以下架")
    skill.status = "offline"
    skill.updated_at = _now()
    await db.commit()
    await db.refresh(skill)
    return skill


async def validate_saved_skill(db: AsyncSession, owner_id: int, skill_id: int) -> dict:
    """校验已保存内容；只读，不改变状态（P08 ValidateButton）。"""
    skill = await _get_owned_skill(db, owner_id, skill_id)
    return validate_skill(_content_dict(skill))


async def delete_skill(db: AsyncSession, owner_id: int, skill_id: int) -> None:
    skill = await _get_owned_skill(db, owner_id, skill_id)
    bound_count = await db.scalar(
        select(func.count()).select_from(ExpertSkill).where(ExpertSkill.skill_id == skill_id)
    )
    if int(bound_count or 0) > 0:
        raise SkillBoundError("Skill 已绑定到专家，请先解绑再删除")
    await db.delete(skill)
    await db.commit()
