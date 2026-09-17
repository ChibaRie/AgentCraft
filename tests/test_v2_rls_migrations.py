"""RLS 增量面（Phase 6 T2 内部服务表 / Phase 7 的 0009 admin 写授权矩阵）。

Phase 9 T6 拆分自 test_v2_rls.py；共享助手见 v2_rls_helpers。
"""

import pytest
from sqlalchemy import text

from tests.conftest import ADMIN_ROLE, APP_ROLE, PgDb
from tests.v2_rls_helpers import (
    _role_engine,
    _seed_bare_user,
    _seed_user_with_task,
)

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
    "experts",
    "skills",
)


# ---- Phase 6 T2（D8 防漂移）：内部服务表无 RLS ----
# task_reservations 与配额/槽位/存储/幂等/限流同类的内部服务表（DB §3.1 内部表，
# 由 app role 同事务直写、无 owner 隔离语义）。清单与全库 RLS 表白名单双向钉死：
# 任何一侧漂移（给内部表加 RLS / 新增 RLS 表未扩名单）即红。
INTERNAL_SERVICE_TABLES = (
    "task_reservations",
    "user_quotas",
    "user_quota_usage",
    "platform_slots",
    "platform_storage",
    "usage_daily",
    "idempotency_records",
    "rate_limit_events",
)

# 0001（14）+ 0003（users/user_entitlements）+ 0006（revision_tools）= 17 张
# + 0013（Phase 10 M1：user_mcp_servers / user_mcp_tools）= 19 张
RLS_ENABLED_TABLES = frozenset(
    {
        "account_action_tokens",
        "email_outbox",
        "expert_revisions",
        "experts",
        "reports",
        "revision_tools",
        "sessions",
        "skill_revisions",
        "skills",
        "task_events",
        "task_files",
        "task_messages",
        "task_rounds",
        "tasks",
        "user_entitlements",
        "user_mcp_servers",
        "user_mcp_tools",
        "user_providers",
        "users",
    }
)


async def test_internal_service_tables_have_no_rls(pg: PgDb, role_engine) -> None:
    """内部服务表 relrowsecurity/relforcerowsecurity 双假且零 policy；app role
    无 owner 上下文可直读其中行（行为面证据——RLS 表同形态查询为 0 行）；
    全库 relrowsecurity=true 的表集合恰为 17 张白名单。"""
    names = ",".join(f"'{t}'" for t in INTERNAL_SERVICE_TABLES)
    async with pg.engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    f"WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname IN ({names})"
                )
            )
        ).all()
        policies = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_policies "
                    f"WHERE schemaname = 'public' AND tablename IN ({names})"
                )
            )
        ).scalar_one()
        rls_tables = {
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity"
                    )
                )
            ).all()
        }
    assert {r[0] for r in rows} == set(INTERNAL_SERVICE_TABLES)
    for relname, rls, force in rows:
        assert (rls, force) == (False, False), relname
    assert policies == 0
    assert rls_tables == RLS_ENABLED_TABLES

    # 行为面：superuser 造 task + task_reservations 行，app role 无 owner 上下文直读可见
    await _seed_user_with_task(pg, "no-rls@x.com")
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO task_reservations (id, task_id, user_id, kind, bytes, state) "
                "SELECT gen_random_uuid(), t.id, t.owner_id, 'active', 0, 'held' FROM tasks t"
            )
        )
    app = role_engine(APP_ROLE)
    try:
        async with app.connect() as conn:  # 无 set_current_owner：无 RLS 即整表可见
            n = (await conn.execute(text("SELECT count(*) FROM task_reservations"))).scalar_one()
            assert n == 1
    finally:
        await app.dispose()


# ---- Phase 7（0009）：admin 写授权矩阵（行值真实变化断言——0006 静默 0 行陷阱的反面）----


async def test_admin_updates_users_status_after_0009(pg: PgDb) -> None:
    """0009 users_admin_update：admin 翻转 users.status，行值真实变化（此前无适用
    policy → 静默 0 行）。矩阵边界一并钉死：users 仅授 UPDATE——admin INSERT 报
    42501（RLS 默认拒绝的 INSERT 形态：新行不满足任何 policy）、DELETE 静默 0 行。"""
    uid = await _seed_bare_user(pg, "adm-status@x.com")
    admin = _role_engine(pg, ADMIN_ROLE)
    try:
        async with admin.begin() as conn:
            updated = await conn.execute(
                text("UPDATE users SET status = 'suspended' WHERE id = :u"), {"u": uid}
            )
            assert updated.rowcount == 1
            status = (
                await conn.execute(text("SELECT status FROM users WHERE id = :u"), {"u": uid})
            ).scalar_one()
            assert status == "suspended"  # 行值真实变化（非仅不抛错）
            deleted = await conn.execute(text("DELETE FROM users"))
            assert deleted.rowcount == 0  # 无 admin DELETE policy → RLS 默认拒绝
        async with admin.connect() as conn:  # 负例独立连接块（单块单失败纪律）
            with pytest.raises(Exception, match="row-level security policy") as excinfo:
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, password_hash, role, status) "
                        "VALUES (gen_random_uuid(), 'adm-ins@x.com', 'h', 'admin', 'active')"
                    )
                )
            assert excinfo.value.orig.sqlstate == "42501"
    finally:
        await admin.dispose()


async def test_admin_entitlement_write_paths_after_0009(pg: PgDb) -> None:
    """0009 user_entitlements admin INSERT/UPDATE/DELETE（0003:63-64 预留兑现）：
    行值真实变化断言——插入（server_default granted_at 落值）→ UPDATE 置
    revoked_at（撤销语义）→ DELETE 移除（行数归零）。"""
    uid = await _seed_bare_user(pg, "adm-ent@x.com")
    admin = _role_engine(pg, ADMIN_ROLE)
    try:
        async with admin.begin() as conn:
            ent_id = (
                await conn.execute(
                    text(
                        "INSERT INTO user_entitlements (id, user_id, entitlement) "
                        "VALUES (gen_random_uuid(), :u, 'expert_author') RETURNING id"
                    ),
                    {"u": uid},
                )
            ).scalar_one()
            granted = (
                await conn.execute(
                    text("SELECT granted_at FROM user_entitlements WHERE id = :e"), {"e": ent_id}
                )
            ).scalar_one()
            assert granted is not None  # server_default now() 生效
            revoked = await conn.execute(
                text("UPDATE user_entitlements SET revoked_at = now() WHERE id = :e"),
                {"e": ent_id},
            )
            assert revoked.rowcount == 1
            value = (
                await conn.execute(
                    text("SELECT revoked_at FROM user_entitlements WHERE id = :e"), {"e": ent_id}
                )
            ).scalar_one()
            assert value is not None
            deleted = await conn.execute(
                text("DELETE FROM user_entitlements WHERE id = :e"), {"e": ent_id}
            )
            assert deleted.rowcount == 1
            remaining = (
                await conn.execute(
                    text("SELECT count(*) FROM user_entitlements WHERE id = :e"), {"e": ent_id}
                )
            ).scalar_one()
            assert remaining == 0
    finally:
        await admin.dispose()


async def test_admin_inserts_email_outbox_after_0009(pg: PgDb) -> None:
    """0009 email_outbox_admin_insert：admin 可插 user_id=NULL 行（邀请场景——
    app 插入路径被 email_outbox_app_insert 的 WITH CHECK (user_id =
    current_owner_id()) 堵死，NULL 不匹配任何 owner → 42501，负例一并钉死）。"""
    admin = _role_engine(pg, ADMIN_ROLE)
    app = _role_engine(pg, APP_ROLE)
    try:
        async with admin.begin() as conn:
            inserted = await conn.execute(
                text(
                    "INSERT INTO email_outbox (id, user_id, purpose, payload_ciphertext, "
                    "state, attempts) VALUES (gen_random_uuid(), NULL, 'invitation', "
                    "'{}', 'pending', 0)"
                )
            )
            assert inserted.rowcount == 1
        async with app.connect() as conn:  # 负例独立连接块
            with pytest.raises(Exception, match="row-level security policy") as excinfo:
                await conn.execute(
                    text(
                        "INSERT INTO email_outbox (id, user_id, purpose, payload_ciphertext, "
                        "state, attempts) VALUES (gen_random_uuid(), NULL, 'invitation', "
                        "'{}', 'pending', 0)"
                    )
                )
            assert excinfo.value.orig.sqlstate == "42501"
    finally:
        await admin.dispose()
        await app.dispose()
