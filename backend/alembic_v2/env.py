import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from backend.v2.models import Base

config = context.config

_url = os.environ.get("AGENTCRAFT_V2_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
if not _url:
    raise RuntimeError("AGENTCRAFT_V2_DATABASE_URL 未设置，拒绝猜测迁移目标")
config.set_main_option("sqlalchemy.url", _url)


def _run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _async_run() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


asyncio.run(_async_run())
