"""V2 迁移链验收：0001（schema+roles+RLS）+ 0002（种子）+ 0003（identity grants/RLS/列）。

直接对 testcontainer PG 建一次性库跑 alembic 子进程（不经模板库克隆），
验证 upgrade/downgrade/upgrade 往返幂等与种子/角色齐备。
注意：与 brief 逐字一致，仅去掉未使用的 import pytest（ruff F401）。
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(dsn: str, *args: str) -> None:
    env = {**os.environ, "AGENTCRAFT_V2_DATABASE_URL": dsn}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic_v2.ini", *args],
        cwd=ROOT,
        env=env,
        check=True,
    )


async def _create_db(base: str, name: str) -> None:
    eng = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await eng.dispose()


async def _drop_db(base: str, name: str) -> None:
    eng = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await eng.dispose()


def test_upgrade_downgrade_upgrade_cycle(pg_url_base):
    name = "ac_mig_" + uuid.uuid4().hex[:8]
    dsn = f"{pg_url_base}/{name}"
    asyncio.run(_create_db(pg_url_base, name))
    try:
        _run_alembic(dsn, "upgrade", "head")
        _run_alembic(dsn, "downgrade", "base")
        _run_alembic(dsn, "upgrade", "head")
    finally:
        asyncio.run(_drop_db(pg_url_base, name))


def test_seeds_and_roles_present(pg_url_base):
    name = "ac_seed_" + uuid.uuid4().hex[:8]
    dsn = f"{pg_url_base}/{name}"
    asyncio.run(_create_db(pg_url_base, name))
    try:
        _run_alembic(dsn, "upgrade", "head")

        async def _check():
            eng = create_async_engine(dsn)
            async with eng.connect() as conn:
                ver = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
                assert ver == "0003"
                slots = (
                    await conn.execute(text("SELECT count(*) FROM platform_slots"))
                ).scalar_one()
                storage = (
                    await conn.execute(
                        text("SELECT max_retained_storage_bytes FROM platform_storage")
                    )
                ).scalar_one()
                tools = (await conn.execute(text("SELECT count(*) FROM tool_catalog"))).scalar_one()
                catalogs = (
                    await conn.execute(text("SELECT count(*) FROM provider_catalog"))
                ).scalar_one()
                roles = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_roles "
                            "WHERE rolname IN ('agentcraft_app','agentcraft_admin')"
                        )
                    )
                ).scalar_one()
            await eng.dispose()
            assert (slots, storage, tools, catalogs, roles) == (2, 64424509440, 5, 3, 2)

        asyncio.run(_check())
    finally:
        asyncio.run(_drop_db(pg_url_base, name))
