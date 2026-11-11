"""V2 运行时：双 DB role 引擎与 owner 上下文。

双 role 分面（裁决 A2）：pre-auth 查找（会话/邀请/token 按 hash、按 email 找用户）
走 admin role（*_admin_read USING(true)）；一切 owner 业务写经 owner_session——
**单事务**内先 set_current_owner 再操作（GUC 事务本地，Phase 1 收口注意事项）。
"""

import ipaddress
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.config import get_settings
from backend.v2.db import build_engine, session_factory
from backend.v2.task_storage import TaskStorage

if TYPE_CHECKING:
    from backend.v2.task_executor import RoundExecutor


@dataclass
class V2Runtime:
    app_factory: async_sessionmaker
    admin_factory: async_sessionmaker
    engines: tuple[AsyncEngine, AsyncEngine]
    # V2 任务物理存储（Phase 6 D10）。默认根仅兜底既有构造点（测试 make_v2_runtime，
    # 不触 I/O）；生产经 v2_runtime_from_settings 传入配置根（V2_TASK.storage_root →
    # <HOST_DATA_ROOT>/task-storage/ 派生）。
    storage: TaskStorage = field(default_factory=lambda: TaskStorage(Path("./data/task-storage")))
    # T6b：RoundExecutor 句柄（main.py lifespan 生产接线 / 测试 setattr 注入；
    # None = 未接线——dispatch notify/executor_loop/注销放弃通知均按缺位降级）。
    # 字符串注解防循环导入（task_executor 反向 import 本模块）。
    executor: "RoundExecutor | None" = None

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
            # D10：纯函数构造，目录由 TaskStorage 方法内惰性创建（无启动 I/O）
            storage=TaskStorage(s.V2_TASK.storage_root or (s.HOST_DATA_ROOT / "task-storage")),
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


async def get_optional_v2_runtime(request: Request) -> V2Runtime | None:
    """V2 可选弱依赖：未配置双 DSN 返回 None（不 503）——供 V1 面的目录校验等
    附加门使用；V2-only 功能不得用它（那是 get_v2_runtime 的 503 语义）。"""
    return v2_runtime_from_settings()


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


@lru_cache(maxsize=8)
def _trusted_proxy_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """解析 TRUSTED_PROXY_IPS（逗号分隔 IP/CIDR）；非法项 fail-closed 忽略。"""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def client_ip(request: Request) -> str:
    """客户端 IP：默认 socket peer；仅当 peer 在 TRUSTED_PROXY_IPS 内时采信
    X-Forwarded-For 首值（nginx 覆盖头），非法头回退 peer。"""
    peer = request.client.host if request.client else ""
    trusted_raw = get_settings().TRUSTED_PROXY_IPS
    if peer and trusted_raw:
        try:
            peer_ip = ipaddress.ip_address(peer)
        except ValueError:
            peer_ip = None
        if peer_ip is not None and any(
            peer_ip in net for net in _trusted_proxy_networks(trusted_raw)
        ):
            forwarded = request.headers.get("x-forwarded-for", "")
            first = forwarded.split(",", 1)[0].strip()
            try:
                candidate = ipaddress.ip_address(first)
            except ValueError:
                candidate = None
            if candidate is not None:
                return str(candidate)
    return peer or "unknown"
