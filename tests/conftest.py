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


@pytest.fixture(autouse=True)
def neutralize_v2_env(monkeypatch):
    """dev .env 的 V2 双 DSN 与四把密钥材料不得进入测试进程（与 CI 无 .env 行为一致）。

    必须 setenv("") 而非 delenv：delenv 后 pydantic-settings 回落读取 CWD 下
    .env 文件值（dev 机泄漏复现路径）。双空 DSN 经 _validate_v2_secrets 的
    v2_mode 判定 → V1-only 形态。需要 V2 DB 的测试一律经 make_v2_runtime
    + 依赖 override 注入，与 Settings 环境无关；个别需测 V2 配置校验的用例在
    测试内自行 monkeypatch.setenv 覆盖本夹具（用例级 monkeypatch 晚于 autouse）。

    中和范围（PlanD-T1/T4 实测交接，超出 brief 的双 DSN）：dev .env 的
    MFA_ENCRYPTION_KEY / EMAIL_OUTBOX_ENCRYPTION_KEY / RATE_LIMIT_HMAC_KEY /
    PROVIDER_KEY_ENCRYPTION_KEY 同样透读测试进程（打破 outbox、models_catalog
    等用例的「未配置」语义），一并 setenv("") 钉空。

    SESSION_COOKIE_SECURE 同理（Phase 4 T10 收口补）：dev .env 为前端手测
    临时置 false（手动浏览器联调 http://127.0.0.1 需要），该手测值不入测试
    进程——钉回 config 默认 true，否则 login/session/config 安检等用例
    （test_v2_login_mfa / test_v2_session_service / test_config_security /
    test_v2_auth_api）按 false 断言失败。dev .env 的 false 保留勿动。
    注意：bool 字段 setenv("") 无法通过 pydantic bool 解析（Input should be
    a valid boolean），故 setenv("true") 显式钉默认值——与 DSN setenv("")
    同理，OS env 优先于 .env 透读。
    """
    monkeypatch.setenv("V2_DATABASE_URL", "")
    monkeypatch.setenv("V2_ADMIN_DATABASE_URL", "")
    monkeypatch.setenv("MFA_ENCRYPTION_KEY", "")
    monkeypatch.setenv("EMAIL_OUTBOX_ENCRYPTION_KEY", "")
    monkeypatch.setenv("RATE_LIMIT_HMAC_KEY", "")
    monkeypatch.setenv("PROVIDER_KEY_ENCRYPTION_KEY", "")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")


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
        self.ignore_abort = False  # T6b：True 时 abort 只记账不生效（bounded-stop forced 路径注入）
        self._round_cancelled = False  # 仅取消当前轮；新一轮不受影响
        self.closed = False
        self.fail_after_prompt = False
        self.crash_on_prompts = 0  # >0 时该次数内 prompt 后 EOF（模拟容器崩溃）
        self._prompt_seen = 0
        self.release: threading.Event | None = None
        self.on_round_start = None
        # Phase 8 T7（§5.6 转办「running 用例 sleep 清理」）：事件驱动等待面——
        # prompt_written 供测试零轮询等待装配完成；round_tasks 登记 detached 轮
        # 任务（测试收尾 await 真实退出，替代固定 sleep）。纯加法：V1 消费面不变。
        self.prompt_written = asyncio.Event()
        self.round_tasks: set[asyncio.Task] = set()
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()

    def emit(self, frame: dict) -> None:
        self._lines.put_nowait(json.dumps(frame, ensure_ascii=False))

    def emit_eof(self) -> None:
        self._lines.put_nowait(None)

    async def write_line(self, line: str) -> None:
        cmd = json.loads(line)
        self.written.append(cmd)
        if cmd.get("type") == "prompt":
            self.prompt_written.set()
            self.emit({"id": cmd["id"], "type": "response", "command": "prompt", "success": True})
            self._prompt_seen += 1
            if self.crash_on_prompts and self._prompt_seen <= self.crash_on_prompts:
                self.emit_eof()  # 容器崩溃：ACK 后 stdout EOF，交 manager 恢复
                return
            self._schedule_round(cmd["message"])
        elif cmd.get("type") == "abort":
            self.aborted = True
            if self.ignore_abort:
                return  # T6b：abort 不生效（轮挂至 engine.stop，forced 收尾路径）
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

        task = asyncio.get_running_loop().create_task(run())
        self.round_tasks.add(task)
        task.add_done_callback(self.round_tasks.discard)

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
        return {
            "source": "system",
            "protocol": "openai",
            "base_url": "http://provider-proxy:8080/v1",
            "model_id": "gpt-4o-mini",
        }

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


@pytest.fixture(autouse=True)
def v2_executor_streams_hygiene():
    """executor/streams 模块级登记的同步清场（T6a M-4 交接，T6b 落地）。

    纪律：同步函数、不触碰事件循环（不 cancel/不 await 任何任务）、不做 IO——
    仅对上一用例残留实例做字典/集合级引用清理（asyncio 资源随用例事件循环关闭
    而失效，此处只解除跨用例可达引用）。V1 用例从不构造 executor/registry，
    WeakSet 为空 → 零开销 no-op（V1 基线不拖慢，计时对比见 task-6b-report）。
    """
    yield
    from backend.v2 import task_executor as _te
    from backend.v2 import task_streams as _ts

    try:
        for executor in list(_te.LIVE_EXECUTORS):
            executor.tokens.clear()
            executor._inflight.clear()
            executor._renewals.clear()
            executor._engines.clear()
            executor._removals.clear()
            executor._rounds.clear()
            executor._detached_tasks.clear()
            while True:
                try:
                    executor.notify_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
    except Exception:  # noqa: BLE001 - 清场夹具绝不破坏测试进程
        pass
    try:
        for registry in list(_ts.LIVE_REGISTRIES):
            registry._subs.clear()
    except Exception:  # noqa: BLE001
        pass


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


# ---------- V2 PostgreSQL 夹具（仅 v2 测试请求；需要本机 Docker）----------
import asyncio
import os
import subprocess
import sys
import uuid as _uuid
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

_AGENTCRAFT_ROOT = Path(__file__).resolve().parents[1]

_APP_ROLE = ("agentcraft_app", "change-me-app-local")
_ADMIN_ROLE = ("agentcraft_admin", "change-me-admin-local")
APP_ROLE = _APP_ROLE
ADMIN_ROLE = _ADMIN_ROLE


def _strip_dbname(dsn: str) -> str:
    """postgresql+asyncpg://u:p@h:port/db -> postgresql+asyncpg://u:p@h:port（不含库名）"""
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _role_dsn(base_dsn: str, user: str, password: str) -> str:
    head, _, tail = base_dsn.partition("://")
    _, _, hostpart = tail.rpartition("@")
    return f"{head}://{user}:{password}@{hostpart}"


def _admin_engine(base_dsn: str):
    # 建/删库用 AUTOCOMMIT，绕开 PG「不能在事务块内 CREATE/DROP DATABASE」限制
    return create_async_engine(base_dsn, isolation_level="AUTOCOMMIT")


@pytest.fixture(scope="session")
def pg_url_base():
    custom = os.environ.get("AGENTCRAFT_TEST_PG_URL")
    if custom:
        yield _strip_dbname(custom.rstrip("/"))
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        raw = pg.get_connection_url()  # postgresql+psycopg2://test:test@host:port/test（自带库名）
        async_url = raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        yield _strip_dbname(async_url)


def _run_migrations(dsn: str) -> None:
    env = {**os.environ, "AGENTCRAFT_V2_DATABASE_URL": dsn}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic_v2.ini", "upgrade", "head"],
        cwd=_AGENTCRAFT_ROOT,
        env=env,
        check=True,
    )


def _template_db_name() -> str:
    """模板库名，可经 AC_TEMPLATE_DB_NAME 覆写（默认 ac_template_v2 不变）。

    并行 pytest 会话经不同模板库名隔离：各会话只建/删/克隆自己的模板库，
    防止对同一模板库 DROP WITH (FORCE) + CREATE 互毁；建模板（pg_template）
    与克隆（_clone_from_template）必须取同一名字，克隆才跟随本会话的模板。"""
    return os.environ.get("AC_TEMPLATE_DB_NAME", "ac_template_v2")


@pytest.fixture(scope="session")
def pg_template(pg_url_base):
    """建模板库并跑 v2 迁移；注意：Task 6 之前 versions/ 为空，upgrade 是无害空操作。"""
    tpl = _template_db_name()
    admin = _admin_engine(pg_url_base)

    async def _recreate():
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{tpl}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{tpl}"'))

    asyncio.run(_recreate())
    asyncio.run(admin.dispose())
    _run_migrations(f"{pg_url_base}/{tpl}")
    yield pg_url_base


class PgDb(NamedTuple):
    engine: AsyncEngine  # superuser 引擎（造数据用，绕过 RLS；普通事务即可）
    base_url: str  # 维护 DSN（不含库名）
    name: str  # 本测试库名

    def url(self) -> str:
        return f"{self.base_url}/{self.name}"

    def role_url(self, user: str, password: str) -> str:
        return _role_dsn(self.url(), user, password)


async def _clone_from_template(base_url: str, name: str) -> None:
    admin = _admin_engine(base_url)
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{_template_db_name()}"'))
    await admin.dispose()


async def _drop_db(base_url: str, name: str) -> None:
    admin = _admin_engine(base_url)
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()


@pytest.fixture
async def pg(pg_template) -> PgDb:
    base = pg_template
    name = f"ac_test_{_uuid.uuid4().hex[:10]}"
    await _clone_from_template(base, name)
    db = PgDb(create_async_engine(f"{base}/{name}"), base, name)
    yield db
    await db.engine.dispose()
    await _drop_db(base, name)


def make_role_engine(pg: PgDb, role: tuple[str, str]) -> AsyncEngine:
    """以指定 DB 角色（app/admin）建当前测试库的引擎池（Phase 6 T2 收编
    test_v2_rls._role_engine 为共享实现；调用方负责 dispose 返回的引擎）。"""
    return create_async_engine(pg.role_url(*role))


@pytest.fixture
def role_engine(pg: PgDb):
    """工厂夹具：返回 make(role) -> AsyncEngine 闭包（T5-T8 双 role 断言复用）。"""

    def make(role: tuple[str, str]) -> AsyncEngine:
        return make_role_engine(pg, role)

    return make


@pytest.fixture
async def pg_fresh(pg) -> PgDb:
    """Task 2-5 模型测试用：按当前模型建表并保证空库起点。

    Task 6 之后模板库已含迁移 schema 与 0002 种子（provider/tool/槽位/存储）：
    create_all 为 checkfirst 空操作，但种子行会随克隆库进入测试库——
    按元数据依赖逆序清空全部表行，维持模型测试假设的空库
    （Task 7 的 pg 夹具不走此处，种子原样保留）。"""
    from backend.v2.models import Base

    async with pg.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    return pg


# Provider 域共享夹具 re-export：普通模块夹具不被 pytest 自动发现，经本 conftest
# 命名空间使其对 tests/ 下全部测试可见（T7-T12 复用；冗余 as 为显式 re-export 惯例）。
# 必须置于文件末尾：helpers → test_v2_runtime 反向 import 本模块的 ADMIN_ROLE/
# APP_ROLE/PgDb，早置会撞循环导入。
from tests.v2_provider_helpers import provider_env as provider_env  # noqa: E402
