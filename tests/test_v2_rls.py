"""V2 RLS 强制执行测试（app/admin 双 role）— 越权矩阵的 DB 层证据。

针对 Task 6 迁移后的真实 schema（0001 roles+policies+RLS、0002 seeds，随模板库
克隆进入每个测试库）验证：

- app role 未设 owner 上下文 / 陌生 owner → SELECT 0 行（RLS USING 过滤）
- app role 设 own owner → 可见；不改 owner_id 的 UPDATE 正常，改写 owner_id 被
  WITH CHECK 拒绝（抛错，而非 SELECT 不可见的静默 0 行）
- app role 无 DDL：CREATE TABLE 被权限拒绝（REVOKE CREATE ON SCHEMA public）
- admin role 经显式 policy 读全量；无 UPDATE/DELETE policy → RLS 默认拒绝 =
  静默 0 行受影响（PostgreSQL 不抛错——brief 的 pytest.raises 版本据此修正为
  rowcount 断言）
- 未知角色无法连接（PUBLIC 表权限已全撤、角色白名单封闭）
- 目录契约（Task 6 评审补充）：policy 总数 ≥54；admin 角色专属 policy ≥12 且
  app 角色不持有任何 *_admin_* policy；app 授权表白名单 26 张表；9 张 owner
  表 relrowsecurity/relforcerowsecurity 双真（FORCE：表 owner 亦受 RLS 约束）

范围说明：idempotency_records / rate_limit_events / account_action_tokens /
email_outbox 仅纳入目录断言（flag/policy 计数），不做行为矩阵——其 owner 流程
尚未落地，Task 6 评审的实机探针已覆盖。

与 brief 的差异（适配真实 schema，详见任务报告）：
1. `from tests.conftest import ...`（tests/ 为包，bare `conftest` 不可导入）
2. seed INSERT 补 user_providers.is_default 与 tasks.event_sequence（两列
   NOT NULL 且无 server default，模型侧 default 只在 ORM 层生效）
3. admin 只读断言从 pytest.raises 改为 rowcount==0 + superuser 复核数据仍在
"""

import uuid as _uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import ADMIN_ROLE, APP_ROLE, PgDb

pytestmark = [pytest.mark.usefixtures("pg")]

OWNER_TABLES = (
    "tasks",
    "task_files",
    "task_messages",
    "task_rounds",
    "task_events",
    "user_providers",
    "sessions",
    "account_action_tokens",
    "email_outbox",
)


def _role_engine(pg: PgDb, role: tuple[str, str]) -> AsyncEngine:
    """以指定角色（app/admin）建连接池，指向当前测试库。"""
    return create_async_engine(pg.role_url(*role))


async def _seed_user_with_task(pg: PgDb, email: str) -> _uuid.UUID:
    """superuser 造最小合法链：user → provider → expert → revision → task。

    superuser 只绕过 RLS、不绕过 FK 与 NOT NULL，因此必须用真实外键链
    （目录行来自 0002 种子），并显式给无 server default 的 NOT NULL 列
    （user_providers.is_default / tasks.event_sequence）赋值。
    """
    async with pg.engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), :e, 'h', 'user', 'active') RETURNING id"
                ),
                {"e": email},
            )
        ).scalar_one()
        catalog_id = (
            await conn.execute(text("SELECT id FROM provider_catalog LIMIT 1"))
        ).scalar_one()
        provider_id = (
            await conn.execute(
                text(
                    "INSERT INTO user_providers (id, user_id, catalog_id, model_id, "
                    "key_ciphertext, dek_wrapped, key_last4, key_version, status, "
                    "is_default) VALUES (gen_random_uuid(), :u, :c, 'm', 'ct', 'dw', "
                    "'4KEY', 1, 'active', false) RETURNING id"
                ),
                {"u": user_id, "c": catalog_id},
            )
        ).scalar_one()
        expert_id = (
            await conn.execute(
                text(
                    "INSERT INTO experts (id, owner_id, status) "
                    "VALUES (gen_random_uuid(), :u, 'published') RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
        revision_id = (
            await conn.execute(
                text(
                    "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) VALUES (gen_random_uuid(), :x, "
                    ":u, 1, '{}', :h, 'published') RETURNING id"
                ),
                {"x": expert_id, "u": user_id, "h": "a" * 64},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE experts SET published_revision_id = :r WHERE id = :x"),
            {"r": revision_id, "x": expert_id},
        )
        await conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, expert_revision_id, provider_id, "
                "provider_catalog_id, provider_model_id, provider_key_version, "
                "event_sequence, status) VALUES (gen_random_uuid(), :u, :r, :p, :c, "
                "'m', 1, 0, 'uploading')"
            ),
            {"u": user_id, "r": revision_id, "p": provider_id, "c": catalog_id},
        )
    return user_id


async def test_app_role_cannot_see_other_owners_tasks(pg: PgDb) -> None:
    """陌生 owner 作用域下 SELECT 0 行：RLS USING 不可见。"""
    await _seed_user_with_task(pg, "a@x.com")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(gen_random_uuid())"))
            n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
            assert n == 0
    finally:
        await app.dispose()


async def test_app_role_sees_own_rows_after_set_current_owner(pg: PgDb) -> None:
    """own owner 作用域可见且可写；owner_id 改写被 WITH CHECK 拒绝。"""
    user_id = await _seed_user_with_task(pg, "b@x.com")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": user_id})
            n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
            assert n == 1
            # 对照组：不动 owner_id 的 UPDATE 照常生效——证明 UPDATE 权限本身存在，
            # 下一条的报错只能来自 WITH CHECK 对新行的拒绝
            updated = await conn.execute(text("UPDATE tasks SET status = 'queued'"))
            assert updated.rowcount == 1
            with pytest.raises(Exception, match="row-level security policy"):
                await conn.execute(text("UPDATE tasks SET owner_id = gen_random_uuid()"))
    finally:
        await app.dispose()


async def test_app_role_has_no_ddl_and_cannot_read_without_context(pg: PgDb) -> None:
    """未设 owner → current_owner_id() 为 NULL → USING 不匹配 0 行；无 DDL。"""
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
            assert n == 0
            with pytest.raises(Exception, match="permission denied"):
                await conn.execute(text("CREATE TABLE sneaky (id int)"))
    finally:
        await app.dispose()


async def test_admin_role_reads_all_via_explicit_policy(pg: PgDb) -> None:
    """admin 经 tasks_admin_read USING(true) 读全量；无 UPDATE/DELETE policy →
    RLS 默认拒绝是静默 0 行受影响（不抛错），数据原样保留。"""
    await _seed_user_with_task(pg, "c@x.com")
    admin = _role_engine(pg, ADMIN_ROLE)
    try:
        async with admin.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
            assert n == 1  # 无需 owner 上下文
            updated = await conn.execute(text("UPDATE tasks SET status = 'completed'"))
            assert updated.rowcount == 0
            deleted = await conn.execute(text("DELETE FROM tasks"))
            assert deleted.rowcount == 0
    finally:
        await admin.dispose()
    async with pg.engine.begin() as conn:  # superuser 复核：未被静默删除
        n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
        assert n == 1


async def test_unknown_role_cannot_connect(pg: PgDb) -> None:
    """PUBLIC 已被整体撤销、角色白名单封闭：未知角色连不上。
    （内部服务表 idempotency_records/rate_limit_events 等对 app role 开放 DML、
    无 owner RLS，其不可探测性由「subject 由服务端派生」保证，见补遗 §7）"""
    unknown = create_async_engine(pg.role_url("some_unknown", "nope"))
    try:
        # 池层建连失败走 asyncpg 原生异常（SQLAlchemy 不在 pool.connect 包
        # DBAPIError）：未知角色 → 认证被拒（InvalidPasswordError 等 PostgresError）
        with pytest.raises(asyncpg.PostgresError):
            async with unknown.connect():
                pass
    finally:
        await unknown.dispose()


async def test_policy_catalog_matches_rls_contract(pg: PgDb) -> None:
    """policy 总数 ≥54（9 owner 表 ×5 + revision 表 ×3×2 + reports ×3 = 54）；
    admin 角色专属 policy ≥12（实际 13）；app 角色不持有任何 *_admin_* policy。"""
    async with pg.engine.begin() as conn:
        total = (await conn.execute(text("SELECT count(*) FROM pg_policies"))).scalar_one()
        admin_owned = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE policyname LIKE '%_admin_%' AND roles = '{agentcraft_admin}'"
                )
            )
        ).scalar_one()
        admin_on_app = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    "WHERE policyname LIKE '%_admin_%' AND roles = '{agentcraft_app}'"
                )
            )
        ).scalar_one()
    assert total >= 54
    assert admin_owned >= 12
    assert admin_on_app == 0


async def test_app_grant_whitelist_table_count(pg: PgDb) -> None:
    """app role 表授权白名单：23 张 DML + 3 张只读 = 26 张
    （users/user_entitlements/content_reviews/audit_logs/alembic_version 均无授权）。"""
    async with pg.engine.begin() as conn:
        n = (
            await conn.execute(
                text(
                    "SELECT count(DISTINCT table_name) "
                    "FROM information_schema.role_table_grants "
                    "WHERE grantee = 'agentcraft_app' AND table_schema = 'public'"
                )
            )
        ).scalar_one()
    assert n == 26


async def test_owner_tables_have_rls_enabled_and_forced(pg: PgDb) -> None:
    """9 张 owner 表 relrowsecurity 与 relforcerowsecurity 双真：FORCE 使表 owner
    也受 RLS 约束，杜绝超级属主旁路。表名来自模块常量，无注入面。"""
    names = ",".join(f"'{t}'" for t in OWNER_TABLES)
    async with pg.engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    f"WHERE n.nspname = 'public' AND c.relname IN ({names})"
                )
            )
        ).all()
    assert {r[0] for r in rows} == set(OWNER_TABLES)
    for relname, rls, force in rows:
        assert (rls, force) == (True, True), relname
