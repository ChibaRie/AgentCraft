from pydantic import BaseModel, Field


class ExpertCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    description: str = Field(min_length=10, max_length=100)
    avatar_url: str | None = None
    category: str
    persona: str
    methodology: str
    task_examples: list[dict[str, str]] | None = None


class ExpertUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    avatar_url: str | None = None
    category: str | None = None
    persona: str | None = None
    methodology: str | None = None
    task_examples: list[dict[str, str]] | None = None


class ExpertSkillBindingRequest(BaseModel):
    skill_id: int
    enabled: bool = False


class ExpertMCPBindingRequest(BaseModel):
    server_id: int
    enabled: bool = False


class ExpertMCPUpdateRequest(BaseModel):
    enabled: bool


class ExpertResponse(BaseModel):
    id: int
    name: str
    status: str
    category: str | None = None
