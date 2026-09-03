"""跨路由共享的设置类依赖；测试通过 app.dependency_overrides 覆盖根目录与限额。"""

from functools import lru_cache
from pathlib import Path

from fastapi import Depends

from backend.config import Settings, get_settings
from backend.engine.extension_generator import ExtensionGenerator
from backend.engine.pi_engine_manager import PiEngineManager
from backend.engine.skill_loader import SkillLoader
from backend.services import task_service
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


def get_skill_loader(settings: Settings = Depends(get_settings)) -> SkillLoader:
    """系统提示词组装器（§7.5），64KiB 上限可经 SKILL_PROMPT_MAX_BYTES 调整。"""
    return SkillLoader(max_bytes=settings.SKILL_PROMPT_MAX_BYTES)


def get_extension_generator(settings: Settings = Depends(get_settings)) -> ExtensionGenerator:
    """任务扩展生成器（§7.4）：task.ts 写盘到控制面扩展目录。"""
    return ExtensionGenerator(Path(settings.HOST_DATA_ROOT) / "extensions")


@lru_cache(maxsize=1)
def get_pi_engine_manager() -> PiEngineManager:
    """容器池单例（§7.2 控制面唯一服务）。测试经 dependency_overrides 替换。"""

    settings = get_settings()

    async def fetch_history(task_id: int, limit: int) -> list[dict]:
        return await task_service.fetch_recent_messages(None, task_id, limit)

    return PiEngineManager(
        settings,
        history_fetcher=fetch_history,
        extension_generator=ExtensionGenerator(
            Path(settings.HOST_DATA_ROOT) / "extensions"
        ),
    )
