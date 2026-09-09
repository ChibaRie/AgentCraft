"""Shared test fixtures.

Each test gets an isolated SQLite database (tmp file) created from ORM
metadata. The app's ``get_db`` dependency is overridden so requests never
touch the development database.

``test_db`` 暴露 session_factory，供需要直接播种数据库的测试（如
ExpertSkill 绑定行）使用。
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production-000")
os.environ.setdefault("TASK_TOKEN_SECRET", "test-task-token-secret-not-for-prod-00")
os.environ.setdefault("ALLOW_INSECURE_SECRETS", "true")

# —— 以上三行必须位于本文件所有 backend.* import 之前；下方保持原有内容不动 ——

import asyncio
import json
import threading
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.config import Settings
from backend.database import get_db
from backend.engine.extension_generator import ExtensionGenerator
from backend.engine.pi_engine_manager import PiEngineManager
from backend.main import app
from backend.models import Base

_TEXT_CHUNK = 3  # 与 EchoEngine 相同的分帧粒度，验证前端流式拼帧


class FakePiTransport:
    """脚本化假 Pi（共享测试仿真）：prompt → ACK + 回显整条 outgoing 消息。

    - 回复按 _TEXT_CHUNK 分帧 text_delta，与 EchoEngine 行为一致，
      使 SSE 契约测试无需感知引擎替换
    - ``release``（threading.Event）可令回复暂停，供轮忙 429 测试；``
      on_round_start`` 在轮帧开始时回调
    - 重播种语义测试断言 outgoing 消息本身（含回顾壳），见 test_pi_manager
    """

    def __init__(self) -> None:
        self.written: list[dict] = []
        self.aborted = False  # 曾收到 abort（断言用）
        self._round_cancelled = False  # 仅取消当前轮；新一轮不受影响
        self.closed = False
        self.fail_after_prompt = False
        self.crash_on_prompts = 0  # >0 时该次数内 prompt 后 EOF（模拟容器崩溃）
        self._prompt_seen = 0
        self.release: threading.Event | None = None
        self.on_round_start = None
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()

    def emit(self, frame: dict) -> None:
        self._lines.put_nowait(json.dumps(frame, ensure_ascii=False))

    def emit_eof(self) -> None:
        self._lines.put_nowait(None)

    async def write_line(self, line: str) -> None:
        cmd = json.loads(line)
        self.written.append(cmd)
        if cmd.get("type") == "prompt":
            self.emit({"id": cmd["id"], "type": "response", "command": "prompt", "success": True})
            self._prompt_seen += 1
            if self.crash_on_prompts and self._prompt_seen <= self.crash_on_prompts:
                self.emit_eof()  # 容器崩溃：ACK 后 stdout EOF，交 manager 恢复
                return
            self._schedule_round(cmd["message"])
        elif cmd.get("type") == "abort":
            self.aborted = True
            self._round_cancelled = True
            # 真实 Pi 中止帧序（tests/fixtures/pi_frames/faux_abort.jsonl）：
            # 半截回复以 stopReason=aborted 的 message_end 交付，settled 照常收尾
            self.emit(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "半截"}],
                        "stopReason": "aborted",
                        "usage": {},
                    },
                }
            )
            self.emit({"type": "agent_settled"})

    def _schedule_round(self, message: str) -> None:
        async def run() -> None:
            if self.on_round_start:
                self.on_round_start()
            if self.release is not None:
                while not self.release.is_set():
                    await asyncio.sleep(0.02)
            if self._round_cancelled:
                self._round_cancelled = False
                return  # 当前轮已被 abort 收尾，静默退出；新一轮不受影响
            self.emit({"type": "agent_start"})
            self.emit({"type": "message_end", "message": {"role": "user", "content": message}})
            if self.fail_after_prompt:
                self.emit(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "errorMessage": "boom",
                        },
                    }
                )
            else:
                for start in range(0, len(message), _TEXT_CHUNK):
                    self.emit(
                        {
                            "type": "message_update",
                            "assistantMessageEvent": {
                                "type": "text_delta",
                                "delta": message[start : start + _TEXT_CHUNK],
                            },
                        }
                    )
                self.emit(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": message}],
                            "stopReason": "stop",
                            "usage": {"input": 3, "output": 2},
                        },
                    }
                )
            self.emit({"type": "agent_settled"})

        asyncio.get_running_loop().create_task(run())

    async def readline(self) -> str | None:
        return await self._lines.get()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


def make_scripted_manager(
    tmp_path: Path,
    *,
    round_timeout: int = 5,
    max_concurrent: int = 4,
    max_lifetime_minutes: int = 30,
):
    """构造 PiEngineManager + FakePiTransport 注入（不启动真实容器/子进程）。"""
    settings = Settings(
        PI_RUNTIME="subprocess",
        HOST_DATA_ROOT=str(tmp_path / "data"),
        HOST_WORKSPACE_ROOT=str(tmp_path / "workspaces"),
        PI_ROUND_TIMEOUT_SECONDS=round_timeout,
        PI_MAX_CONCURRENT_CONTAINERS=max_concurrent,
        PI_TASK_MAX_LIFETIME_MINUTES=max_lifetime_minutes,
    )
    transports: list[FakePiTransport] = []

    async def fetch_history(task_id: int, limit: int) -> list[dict]:
        return []

    async def resolve_provider(user_id: int, provider_config_id: int | None) -> dict:
        return {"source": "system", "protocol": "openai",
                "base_url": "http://provider-proxy:8080/v1", "model_id": "gpt-4o-mini"}

    manager = PiEngineManager(
        settings,
        history_fetcher=fetch_history,
        provider_resolver=resolve_provider,
        extension_generator=ExtensionGenerator(tmp_path / "extensions"),
    )

    async def fake_make_runtime(spec, workdir_host, extension_path):
        transport = FakePiTransport()
        transports.append(transport)

        async def noop() -> None:
            return None

        return transport, noop

    manager._make_runtime = fake_make_runtime  # type: ignore[method-assign]
    return manager, transports


class TestDatabase:
    def __init__(self, engine, session_factory):
        self.engine = engine
        self.session_factory = session_factory

    def run(self, coro):
        """在独立事件循环中执行一段协程（播种/查询用）。"""
        return asyncio.run(coro)


@pytest.fixture(autouse=True)
def reset_task_locks():
    """task_locks 是模块级 dict；测试各自独占事件循环，跨测试残留的
    asyncio.Lock 会绑定已死循环（RuntimeError）或保持已锁状态（429）。
    每用例前清空（生产单循环不受影响）。"""
    from backend.services import task_locks

    task_locks._data_locks.clear()
    task_locks._round_locks.clear()
    yield
    task_locks._data_locks.clear()
    task_locks._round_locks.clear()


@pytest.fixture()
def test_db(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'agentcraft-test.db'}",
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async def init_tables() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(init_tables())

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    database = TestDatabase(engine, session_factory)
    yield database
    asyncio.run(engine.dispose())


@pytest.fixture()
def client(test_db):
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with test_db.session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)
