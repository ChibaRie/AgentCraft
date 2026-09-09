"""V2 运行时：双 DB role 引擎与 owner 上下文。

双 role 分面（裁决 A2）：pre-auth 查找（会话/邀请/token 按 hash、按 email 找用户）
走 admin role（*_admin_read USING(true)）；一切 owner 业务写经 owner_session——
**单事务**内先 set_current_owner 再操作（GUC 事务本地，Phase 1 收口注意事项）。
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.v2.db import build_engine, session_factory


@dataclass
class V2Runtime:
    app_factory: async_sessionmaker
    admin_factory: async_sessionmaker
    engines: tuple[AsyncEngine, AsyncEngine]

    def close(self) -> None:
        for engine in self.engines:
            engine.sync_engine.dispose()


_runtime: V2Runtime | None = None


def v2_runtime_from_settings() -> V2Runtime:
    """进程内单例；未配置双 DSN 时返回 None（由依赖转 503）。"""
    global _runtime
    if _runtime is None:
        from backend.config import get_settings

        s = get_settings()
        if not (s.V2_DATABASE_URL and s.V2_ADMIN_DATABASE_URL):
            return None  # type: ignore[return-value]
        app_engine = build_engine(s.V2_DATABASE_URL)
        admin_engine = build_engine(s.V2_ADMIN_DATABASE_URL)
        _runtime = V2Runtime(
            app_factory=session_factory(app_engine),
            admin_factory=session_factory(admin_engine),
            engines=(app_engine, admin_engine),
        )
    return _runtime


async def get_v2_runtime(request: Request) -> V2Runtime:
    """FastAPI 依赖：未配置双 DSN（V1-only 模式）→ 503，不触碰 DB。

    ``request`` 形参对齐接口签名，供未来请求级日志/租户解析使用。
    """
    runtime = v2_runtime_from_settings()
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "V2 数据层未配置"},
        )
    return runtime


async def get_admin_db(
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> AsyncIterator[AsyncSession]:
    """每请求 admin 会话（只读语义）：不 commit，退出即关闭。"""
    async with runtime.admin_factory() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def owner_session(runtime: V2Runtime, user_id: str) -> AsyncIterator[AsyncSession]:
    async with runtime.app_factory() as session:
        async with session.begin():
            await session.execute(text("SELECT app.set_current_owner(:uid)"), {"uid": str(user_id)})
            yield session
