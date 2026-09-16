"""引擎装配依赖（Phase 8 T13 cutover 后收窄）。

V1 应用面删除（D2）后，本模块仅存容器池单例 ``get_pi_engine_manager``：
main.py lifespan 的后台巡检循环与 /internal/harness 冻结面（D15-Option1）
消费。原 workspace/file_service/skill_loader 等依赖随 V1 路由一并消亡；
manager 的 history/provider 回调改为本地内联实现（V1 服务层已删，ORM 模型
保留），回调消费面（run_round/ensure_container 重播种与容器重建）为冻结
保留逻辑。
"""

from functools import lru_cache
from pathlib import Path

from backend.config import get_settings
from backend.engine.extension_generator import ExtensionGenerator
from backend.engine.pi_engine_manager import PiEngineManager


async def _fetch_recent_history(task_id: int, limit: int) -> list[dict]:
    """重播种取数（§7.6）：最近 limit 条持久化消息，时间升序。

    原 backend.services.task_service.fetch_recent_messages 内联收编
    （V1 服务层随 cutover 删除，Conversation/Message 模型保留）。
    """
    from sqlalchemy import select

    from backend.database import async_session_factory
    from backend.models.conversation import Conversation
    from backend.models.message import Message

    async with async_session_factory() as session:
        conversation = (
            await session.execute(select(Conversation).where(Conversation.task_id == task_id))
        ).scalar_one_or_none()
        if conversation is None:
            return []
        rows = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )
        rows.reverse()
        return [{"role": item.role, "content": item.content} for item in rows]


def _build_provider_snapshot(
    *,
    source: str,
    protocol: str,
    base_url: str,
    model_id: str,
    user_provider_id: int | None = None,
    api_key_encrypted: str | None = None,
) -> dict:
    """任务级 Provider 快照（§7.7；原 provider_service.build_provider_snapshot 内联）。"""
    import json
    from datetime import datetime, timezone

    snapshot = {
        "source": source,
        "protocol": protocol,
        "base_url": base_url,
        "model_id": model_id,
        "loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    if source == "user":
        snapshot["user_provider_id"] = user_provider_id
        snapshot["api_key_encrypted"] = json.loads(api_key_encrypted) if api_key_encrypted else None
    return snapshot


async def _resolve_provider_snapshot(
    user_id: int, provider_config_id: int | None, settings
) -> dict:
    """Provider 快照解析（§7.7 回退链）：显式配置 → 用户默认 → 系统默认。

    原 backend.services.provider_service.resolve_task_provider 内联收编。
    """
    from sqlalchemy import select

    from backend.database import async_session_factory
    from backend.models.user_provider import UserProvider

    async with async_session_factory() as session:
        row: UserProvider | None = None
        if provider_config_id is not None:
            candidate = await session.get(UserProvider, provider_config_id)
            if candidate is not None and candidate.user_id == user_id:
                row = candidate
        if row is None:
            row = (
                await session.execute(
                    select(UserProvider).where(
                        UserProvider.user_id == user_id, UserProvider.is_default == 1
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return _build_provider_snapshot(
                source="system",
                protocol=settings.PI_PROVIDER,
                base_url=settings.PI_PROXY_BASE_URL,
                model_id=settings.PI_MODEL,
            )
        return _build_provider_snapshot(
            source="user",
            protocol=row.protocol,
            base_url=row.base_url,
            model_id=row.model_id,
            user_provider_id=row.id,
            api_key_encrypted=row.api_key_encrypted,
        )


async def _fetch_running_tasks() -> list[dict]:
    """§7.8.1 看门狗取数：running 任务的 id 与 running 起点（updated_at）。"""
    from sqlalchemy import select

    from backend.database import async_session_factory
    from backend.models.task import Task

    async with async_session_factory() as session:
        result = await session.execute(
            select(Task.id, Task.updated_at).where(Task.status == "running")
        )
        return [
            {"id": row[0], "running_since": row[1]} for row in result.all() if row[1] is not None
        ]


async def _mark_task_failed(task_id: int) -> None:
    """§7.8.1 看门狗落库：仅 running → failed（不覆盖 completed）。"""
    from datetime import datetime, timezone

    from sqlalchemy import update

    from backend.database import async_session_factory
    from backend.models.task import Task

    async with async_session_factory() as session:
        await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == "running")
            .values(status="failed", updated_at=datetime.now(timezone.utc))
        )
        await session.commit()


@lru_cache(maxsize=1)
def get_pi_engine_manager() -> PiEngineManager:
    """容器池单例（§7.2 控制面唯一服务）。测试经 dependency_overrides 替换。"""

    settings = get_settings()

    async def resolve_provider(user_id: int, provider_config_id: int | None) -> dict:
        return await _resolve_provider_snapshot(user_id, provider_config_id, settings)

    return PiEngineManager(
        settings,
        history_fetcher=_fetch_recent_history,
        provider_resolver=resolve_provider,
        extension_generator=ExtensionGenerator(Path(settings.HOST_DATA_ROOT) / "extensions"),
        running_tasks_fetcher=_fetch_running_tasks,
        mark_task_failed=_mark_task_failed,
    )
