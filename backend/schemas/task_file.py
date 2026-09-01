from pydantic import BaseModel


class FileListResponse(BaseModel):
    files: list[dict[str, object]] = []
