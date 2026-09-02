"""Shared test fixtures.

Each test gets an isolated SQLite database (tmp file) created from ORM
metadata. The app's ``get_db`` dependency is overridden so requests never
touch the development database.

``test_db`` 暴露 session_factory，供需要直接播种数据库的测试（如
ExpertSkill 绑定行）使用。
"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.database import get_db
from backend.main import app
from backend.models import Base


class TestDatabase:
    def __init__(self, engine, session_factory):
        self.engine = engine
        self.session_factory = session_factory

    def run(self, coro):
        """在独立事件循环中执行一段协程（播种/查询用）。"""
        return asyncio.run(coro)


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
