from pydantic import BaseModel, Field


class MCPServerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=200)
    transport: str
    url: str | None = None
    command: str | None = None
    env_vars: dict[str, str] | None = None


class MCPServerUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=50)
    description: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = None
    command: str | None = None
    env_vars: dict[str, str] | None = None  # 提供该字段即整体替换（缺席=不变）


class MCPToolUpdateRequest(BaseModel):
    enabled: bool
    confirm_sensitive: bool = False  # 敏感工具启用需显式确认（§6.7）
