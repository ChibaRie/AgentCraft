"""治理域请求模型（Phase 4：作者面 + 举报；D8 字段边界）。

资源卫生惯例沿用 api/v2/schemas.py（V2BaseModel 基类 + extra="forbid" 写模型 +
Optional/exclude_unset 三态）。内容字段为全量替换语义（revision 快照），不用
exclude_unset；skill_refs/tools 为数组上限收口。
"""

from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from backend.api.v2.schemas import V2BaseModel

_CATEGORIES = Literal["tech", "design", "writing", "data_analysis", "office", "other"]


class SkillRefItem(V2BaseModel):
    skill_id: str = Field(min_length=36, max_length=36)
    revision_id: str = Field(min_length=36, max_length=36)


class ExpertContentPayload(V2BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=30)
    description: str = Field(min_length=10, max_length=100)
    category: _CATEGORIES
    avatar_url: str | None = Field(default=None, max_length=512)
    persona: str = Field(min_length=1, max_length=8000)
    methodology: str = Field(min_length=1, max_length=8000)
    task_examples: list[str] = Field(default_factory=list, max_length=5)
    skill_refs: list[SkillRefItem] = Field(default_factory=list, max_length=20)

    @field_validator("persona", "methodology")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value

    @field_validator("task_examples")
    @classmethod
    def _examples_bounds(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item.strip() or len(item) > 50:
                raise ValueError("task_examples 每条须为 1-50 字符的非空白文本")
        return value

    @field_validator("avatar_url")
    @classmethod
    def _avatar_scheme(cls, value: str | None) -> str | None:
        # V1 ExpertCreateRequest 同款 http/https 门（U1 §2.3）
        if value is not None and not value.startswith(("http://", "https://")):
            raise ValueError("avatar_url 须为 http/https URL")
        return value


class SkillContentPayload(V2BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=30)
    description: str = Field(min_length=10, max_length=200)
    use_case: str = Field(min_length=20, max_length=5000)
    role: str = Field(min_length=5, max_length=200)
    goal: str = Field(min_length=20, max_length=5000)
    steps: str = Field(min_length=20, max_length=5000)
    input_requirements: str | None = Field(default=None, max_length=5000)
    output_requirements: str = Field(min_length=20, max_length=5000)
    constraints: str = Field(min_length=20, max_length=5000)

    @field_validator("use_case", "role", "goal", "steps", "output_requirements", "constraints")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("不能为空白")
        return value


class ToolItem(V2BaseModel):
    tool_id: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=20)


class SubmitPayload(V2BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[ToolItem] = Field(default_factory=list, max_length=20)


class ReportCreatePayload(V2BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["expert_revision", "skill_revision", "message"]
    target_id: str = Field(min_length=36, max_length=36)
    target_revision_hash: str | None = Field(default=None, min_length=64, max_length=64)
    reason: str = Field(min_length=1, max_length=2000)
