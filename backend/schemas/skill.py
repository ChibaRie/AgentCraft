"""Skill 请求/响应 Schema（Engineering Spec §6.5 + PRD §4.4.2 字段规则）。

字段规则（PRD §4.4.2）：
- 名称 2-30 字符（去首尾空格后计长）
- 描述 10-200 字符（去首尾空格后计长）
- 使用场景/任务目标/工作步骤/输出要求/约束 20-5000 字符，不能全空白
- AI 角色 5-200 字符，不能全空白
- 输入要求可选，≤5000 字符
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

_BLANK_SENSITIVE_FIELDS = (
    "use_case",
    "role",
    "goal",
    "steps",
    "output_requirements",
    "constraints",
)


class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=30)
    description: str = Field(min_length=10, max_length=200)
    use_case: str = Field(min_length=20, max_length=5000)
    role: str = Field(min_length=5, max_length=200)
    goal: str = Field(min_length=20, max_length=5000)
    steps: str = Field(min_length=20, max_length=5000)
    input_requirements: str | None = Field(default=None, max_length=5000)
    output_requirements: str = Field(min_length=20, max_length=5000)
    constraints: str = Field(min_length=20, max_length=5000)

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_short_fields(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator(*_BLANK_SENSITIVE_FIELDS)
    @classmethod
    def reject_all_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能全为空白字符")
        return value


class SkillUpdateRequest(BaseModel):
    # 不可清除字段声明为 str（default=None 仅表示"未提供"；Pydantic v2 默认不校验默认值，
    # exclude_unset 仍只收集调用方提供的字段；显式 null 会被类型校验以 400 拒绝，
    # 避免 setattr(None) 触发 NOT NULL 约束的 500）
    name: str = Field(default=None, min_length=2, max_length=30)
    description: str = Field(default=None, min_length=10, max_length=200)
    use_case: str = Field(default=None, min_length=20, max_length=5000)
    role: str = Field(default=None, min_length=5, max_length=200)
    goal: str = Field(default=None, min_length=20, max_length=5000)
    steps: str = Field(default=None, min_length=20, max_length=5000)
    input_requirements: str | None = Field(default=None, max_length=5000)
    output_requirements: str = Field(default=None, min_length=20, max_length=5000)
    constraints: str = Field(default=None, min_length=20, max_length=5000)

    @field_validator("name", "description", mode="before")
    @classmethod
    def strip_short_fields(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator(*_BLANK_SENSITIVE_FIELDS)
    @classmethod
    def reject_all_whitespace(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("不能全为空白字符")
        return value


class SkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    use_case: str
    role: str
    goal: str
    steps: str
    input_requirements: str | None
    output_requirements: str
    constraints: str
    status: str
    created_at: datetime
    updated_at: datetime


class BoundExpertRef(BaseModel):
    id: int
    name: str


class SkillDetailResponse(SkillResponse):
    bound_experts: list[BoundExpertRef]


class ValidationIssue(BaseModel):
    field: str
    rule: str
    level: str
    message: str


class SkillValidateResponse(BaseModel):
    valid: bool
    issues: list[ValidationIssue]


class SkillDeletedResponse(BaseModel):
    message: str
