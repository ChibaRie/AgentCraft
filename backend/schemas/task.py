from pydantic import BaseModel, Field, field_validator


def _strip(value):
    return value.strip() if isinstance(value, str) else value


class TaskCreateRequest(BaseModel):
    expert_id: int
    description: str = Field(min_length=1, max_length=2000)
    workdir: str | None = None
    provider_config_id: int | None = None  # 缺省 → 用户默认 → 系统默认（§7.7）

    @field_validator("description", "workdir", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        return _strip(value)


class TaskMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=32000)

    @field_validator("content", mode="before")
    @classmethod
    def _strip_strings(cls, value):
        return _strip(value)
