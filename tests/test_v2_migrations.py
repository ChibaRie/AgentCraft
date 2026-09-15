"""V2 迁移链验收：0001（schema+roles+RLS）+ 0002（种子）+ 0003（identity grants/RLS/列）
+ 0004（user_providers 活跃条目唯一索引）+ 0005（users 删除期限 CHECK 重命名）
+ 0006（治理域 admin 写路径 policy + revision_tools RLS + owner 护栏触发器）
+ 0007（revision_tools 冻结护栏触发器——非 draft 父行工具集 owner 不可变）
+ 0008（Phase 6 任务域：任务域部分索引/终态意图位/产物轮次列）
+ 0009（Phase 7 admin 写授权矩阵：users UPDATE + entitlements INSERT/UPDATE/DELETE
+ email_outbox INSERT）。

直接对 testcontainer PG 建一次性库跑 alembic 子进程（不经模板库克隆），
验证 upgrade/downgrade/upgrade 往返幂等与种子/角色齐备。
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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
                assert ver == "0009"
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


async def test_0004_active_entry_unique_index(pg):
    """0004：活跃条目 (user_id, catalog_id, model_id) 部分唯一索引（裁决 D4）。"""
    async with pg.engine.begin() as conn:
        defs = (
            (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'uq_user_providers_active_entry'"
                    )
                )
            )
            .scalars()
            .all()
        )
        # 谓词反解形态随 PG 版本可能为 (status = 'active'::text) 或 ((status)::text = ...)，
        # 断言放宽到「含 status 与 'active' 字面」避免反解形态耦合
        assert len(defs) == 1 and "status" in defs[0] and "'active'" in defs[0]
        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), 'u4@x.test', 'h', 'user', 'active') RETURNING id"
                )
            )
        ).scalar_one()
        cat = (await conn.execute(text("SELECT id FROM provider_catalog LIMIT 1"))).scalar_one()
        for status in ("revoked", "revoked", "active"):
            await conn.execute(
                text(
                    "INSERT INTO user_providers (id, user_id, catalog_id, model_id, "
                    "key_ciphertext, dek_wrapped, key_last4, key_version, status, is_default) "
                    "VALUES (gen_random_uuid(), :u, :c, 'm', 'ct', 'dw', '4KEY', 1, :s, false)"
                ),
                {"u": user, "c": cat, "s": status},
            )
    # 第二条 active 同三元组 → 唯一冲突（独立事务：revoked 行不参与约束故前三条可共存）
    with pytest.raises(IntegrityError):
        async with pg.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO user_providers (id, user_id, catalog_id, model_id, "
                    "key_ciphertext, dek_wrapped, key_last4, key_version, status, is_default) "
                    "VALUES (gen_random_uuid(), :u, :c, 'm', 'ct', 'dw', '4KEY', 1, "
                    "'active', false)"
                ),
                {"u": user, "c": cat},
            )
