"""专家请求/响应 Schema（Engineering Spec §6.3/§6.4 + PRD §4.2.2 字段规则）。

字段规则（PRD §4.2.2）：
- 名称 2-30 字符（去首尾空格后计长，不校验唯一性）
- 简介 10-100 字符（去首尾空格后计长）
- 头像可选，http/https URL（可达性由前端加载失败时回退占位头像，后端不主动抓取）
- 分类枚举：tech/design/writing/data_analysis/office/other
- 人设/方法论必填且不能全空白（建议 200-2000 字符，不强制上限）
- 任务示例可选，最多 5 条，每条 ≤50 字符
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ExpertCategory = Literal["tech", "design", "writing", "data_analysis", "office", "other"]

_TASK_EXAMPLE_MAX_ITEMS = 5
_TASK_EXAMPLE_MAX_CHARS = 50


class ExpertCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=30)
    description: str = Field(min_length=10, max_length=100)
    avatar_url: str | None = Field(default=None, max_length=500)
    category: ExpertCategory
    persona: str
    methodology: str
    task_examples: list[str] | None = Field(default=None, max_length=_TASK_EXAMPLE_MAX_ITEMS)

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_short_fields(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("persona", "methodology")
    @classmethod
    def reject_all_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能全为空白字符")
        return value

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_scheme(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("头像必须是 http/https 链接")
        return value

    @field_validator("task_examples")
    @classmethod
    def validate_task_examples(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(len(item) > _TASK_EXAMPLE_MAX_CHARS for item in value):
            raise ValueError(f"任务示例每条不超过 {_TASK_EXAMPLE_MAX_CHARS} 个字符")
        return value


class ExpertUpdateRequest(BaseModel):
    # 非清除字段声明为具体类型（显式 null 会被类型校验以 400 拒绝，见 SkillUpdateRequest 注释）
    name: str = Field(default=None, min_length=2, max_length=30)
    description: str = Field(default=None, min_length=10, max_length=100)
    avatar_url: str | None = Field(default=None, max_length=500)
    category: ExpertCategory = Field(default=None)
    persona: str = Field(default=None)
    methodology: str = Field(default=None)
    task_examples: list[str] | None = Field(default=None, max_length=_TASK_EXAMPLE_MAX_ITEMS)

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_short_fields(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("persona", "methodology")
    @classmethod
    def reject_all_whitespace(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("不能全为空白字符")
        return value

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_scheme(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("头像必须是 http/https 链接")
        return value

    @field_validator("task_examples")
    @classmethod
    def validate_task_examples(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and any(len(item) > _TASK_EXAMPLE_MAX_CHARS for item in value):
            raise ValueError(f"任务示例每条不超过 {_TASK_EXAMPLE_MAX_CHARS} 个字符")
        return value


class ExpertSkillBindingRequest(BaseModel):
    skill_id: int
    enabled: bool = False


class ExpertBindingToggleRequest(BaseModel):
    enabled: bool


class ExpertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    avatar_url: str | None
    category: str
    persona: str
    methodology: str
    task_examples: list[str] | None
    status: str
    created_at: datetime
    updated_at: datetime


class BoundSkillRef(BaseModel):
    id: int
    name: str
    description: str
    status: str
    enabled: bool


class ExpertDetailResponse(ExpertResponse):
    skills: list[BoundSkillRef]


class ExpertBindingResponse(BaseModel):
    expert_id: int
    skill_id: int
    enabled: bool


class DeletedResponse(BaseModel):
    message: str


class UnboundResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# 专家中心（公开面）
# ---------------------------------------------------------------------------


class DiscoverExpertCard(BaseModel):
    id: int
    name: str
    description: str
    avatar_url: str | None
    category: str
    skill_count: int


class DiscoverSkillRef(BaseModel):
    id: int
    name: str
    description: str


class DiscoverExpertDetail(BaseModel):
    id: int
    name: str
    description: str
    avatar_url: str | None
    category: str
    persona: str
    methodology: str
    task_examples: list[str] | None
    skills: list[DiscoverSkillRef]
