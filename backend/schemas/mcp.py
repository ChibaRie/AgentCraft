from pydantic import BaseModel, Field


class MCPServerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=200)
    transport: str
    url: str | None = None
    command: str | None = None
    env_vars: dict[str, str] | None = None


class MCPServerUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    url: str | None = None
    command: str | None = None
    env_vars: dict[str, str] | None = None


class MCPToolUpdateRequest(BaseModel):
    enabled: bool
    authorized: bool | None = None


class MCPServerResponse(BaseModel):
    id: int
    name: str
    status: str
