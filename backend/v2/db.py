"""V2 数据层引擎与 owner 作用域。RLS 上下文经 app.set_current_owner 设置（事务本地）。
注意：asyncpg 单次 execute 只允许一条语句，OWNER_FN_STATEMENTS 必须逐条执行。"""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

# 迁移 0001 逐条执行；此处持有 SQL 文本常量，便于测试与迁移共享
OWNER_FN_STATEMENTS = [
    "CREATE SCHEMA IF NOT EXISTS app",
    """CREATE OR REPLACE FUNCTION app.set_current_owner(p_owner uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = app, pg_temp AS $$
  SELECT set_config('app.current_owner_id', COALESCE(p_owner::text, ''), true);
$$""",
    """CREATE OR REPLACE FUNCTION app.current_owner_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('app.current_owner_id', true), '')::uuid;
$$""",
    "REVOKE ALL ON FUNCTION app.set_current_owner(uuid) FROM PUBLIC",
    "REVOKE ALL ON FUNCTION app.current_owner_id() FROM PUBLIC",
]


def build_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)
