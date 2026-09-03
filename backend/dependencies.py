"""跨路由共享的设置类依赖；测试通过 app.dependency_overrides 覆盖根目录与限额。"""

from pathlib import Path

from fastapi import Depends

from backend.config import Settings, get_settings
from backend.services.file_service import FileService


def get_workspace_root(settings: Settings = Depends(get_settings)) -> Path:
    """授权工作区根目录（宿主机路径），对应 Agent 侧 /workspaces/authorized。"""
    return Path(settings.HOST_WORKSPACE_ROOT)


def get_file_service(settings: Settings = Depends(get_settings)) -> FileService:
    """任务文件存储服务，限额来自环境配置（§6.6 默认 20MB/10 个/100MB）。"""
    return FileService(
        Path(settings.HOST_DATA_ROOT) / "task-files",
        max_single_bytes=settings.UPLOAD_MAX_FILE_BYTES,
        max_files_per_request=settings.UPLOAD_MAX_FILES_PER_REQUEST,
        max_task_bytes=settings.UPLOAD_MAX_TASK_BYTES,
    )
