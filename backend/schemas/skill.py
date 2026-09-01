from pydantic import BaseModel, Field


class SkillCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=50)
    description: str = Field(min_length=1, max_length=200)
    use_case: str
    role: str
    goal: str
    steps: str
    input_requirements: str | None = None
    output_requirements: str
    constraints: str


class SkillUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    use_case: str | None = None
    role: str | None = None
    goal: str | None = None
    steps: str | None = None
    input_requirements: str | None = None
    output_requirements: str | None = None
    constraints: str | None = None


class SkillResponse(BaseModel):
    id: int
    name: str
    status: str


class SkillValidationResponse(BaseModel):
    valid: bool
    issues: list[dict[str, str]] = []
