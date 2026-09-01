from datetime import datetime

from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    expert_id: int
    description: str = Field(min_length=1)
    workdir: str | None = None


class TaskMessageRequest(BaseModel):
    content: str = Field(min_length=1)


class TaskResponse(BaseModel):
    id: int
    title: str
    status: str
    workdir: str
    expert_name_snapshot: str
    created_at: datetime


class TaskFileResponse(BaseModel):
    id: int
    original_name: str
    agent_path: str
    size_bytes: int
    mime_type: str | None = None
    sha256: str
    created_at: datetime
