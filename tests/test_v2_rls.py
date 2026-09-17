"""RLS 核心矩阵（Phase 1；Phase 9 T6 拆分）：A/B/admin 角色可见性、policy 目录、
授权白名单、RLS 强制面、users 行级对称、信任边界固化。

护栏面（0006/0007）见 test_v2_rls_guards.py；Phase 6/7 增量面见
test_v2_rls_migrations.py；共享助手见 v2_rls_helpers。
"""

import uuid as _uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import ADMIN_ROLE, APP_ROLE, PgDb
from tests.v2_rls_helpers import (
    _published_ids_for,
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


async def _seed_skill_with_revision(pg: PgDb, email: str) -> _uuid.UUID:
    """superuser 造最小合法 skills 链：user → skill → revision（published 指针）。"""
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
        skill_id = (
            await conn.execute(
                text(
                    "INSERT INTO skills (id, owner_id, status) "
                    "VALUES (gen_random_uuid(), :u, 'published') RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
        revision_id = (
            await conn.execute(
                text(
                    "INSERT INTO skill_revisions (id, skill_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) VALUES (gen_random_uuid(), :s, "
                    ":u, 1, '{}', :h, 'published') RETURNING id"
                ),
                {"s": skill_id, "u": user_id, "h": "c" * 64},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE skills SET published_revision_id = :r WHERE id = :s"),
            {"r": revision_id, "s": skill_id},
        )
    return user_id


async def test_app_role_cannot_see_other_owners_tasks(pg: PgDb) -> None:
    """陌生 owner 作用域下读写全被 RLS 过滤：SELECT 0 行；UPDATE/DELETE 走默认
    拒绝（无适用 policy → 静默 0 行受影响，不抛错）；superuser 复核数据仍在。
    若 tasks_app_update/delete 的 USING 被放宽为 true，此用例即红。"""
    await _seed_user_with_task(pg, "a@x.com")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(gen_random_uuid())"))
            n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
            assert n == 0
            updated = await conn.execute(text("UPDATE tasks SET status = 'completed'"))
            assert updated.rowcount == 0
            deleted = await conn.execute(text("DELETE FROM tasks"))
            assert deleted.rowcount == 0
    finally:
        await app.dispose()
    async with pg.engine.begin() as conn:  # superuser 复核：数据未被静默改/删
        n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
        assert n == 1


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
            with pytest.raises(Exception, match="row-level security policy") as excinfo:
                await conn.execute(text("UPDATE tasks SET owner_id = gen_random_uuid()"))
            assert excinfo.value.orig.sqlstate == "42501"  # WITH CHECK 拒绝（RLS 违约）
    finally:
        await app.dispose()


async def test_app_role_has_no_ddl_and_cannot_read_without_context(pg: PgDb) -> None:
    """未设 owner → current_owner_id() 为 NULL → USING 不匹配 0 行；无 DDL。"""
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
            assert n == 0
            with pytest.raises(Exception, match="permission denied") as excinfo:
                await conn.execute(text("CREATE TABLE sneaky (id int)"))
            assert excinfo.value.orig.sqlstate == "42501"  # insufficient_privilege
    finally:
        await app.dispose()


async def test_app_role_stranger_sees_published_revision(pg: PgDb) -> None:
    """expert_revisions_app_published_read 正方向：陌生 owner 恰好看见已发布
    revision（EXISTS 经 experts.published_revision_id），发布内容不因 RLS 失明。"""
    await _seed_user_with_task(pg, "a@x.com")  # 1 条 published revision
    stranger = await _seed_bare_user(pg, "stranger@x.com")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": stranger})
            seen = (await conn.execute(text("SELECT id FROM expert_revisions"))).scalar_one()
    finally:
        await app.dispose()
    async with pg.engine.begin() as conn:  # 可见的正是发布链指向的那条
        published = (
            await conn.execute(text("SELECT published_revision_id FROM experts"))
        ).scalar_one()
    assert seen == published


async def test_app_role_stranger_does_not_see_draft_revision(pg: PgDb) -> None:
    """expert_revisions_app_published_read 反方向：同一 expert 的 draft revision
    不泄漏给陌生 owner——superuser 可见 2 条而陌生 owner 仍只见 1 条（已发布）。"""
    await _seed_user_with_task(pg, "a@x.com")  # revision_no=1, published
    stranger = await _seed_bare_user(pg, "stranger@x.com")
    async with pg.engine.begin() as conn:
        expert_id = (await conn.execute(text("SELECT id FROM experts LIMIT 1"))).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                "content_json, content_sha256, status) VALUES (gen_random_uuid(), :x, "
                "(SELECT owner_id FROM experts WHERE id = :x), 2, '{}', :h, 'draft')"
            ),
            {"x": expert_id, "h": "b" * 64},
        )
        total = (await conn.execute(text("SELECT count(*) FROM expert_revisions"))).scalar_one()
    assert total == 2  # draft 确实存在，只是被 RLS 过滤
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": stranger})
            n = (await conn.execute(text("SELECT count(*) FROM expert_revisions"))).scalar_one()
            assert n == 1
            status = (await conn.execute(text("SELECT status FROM expert_revisions"))).scalar_one()
            assert status == "published"
    finally:
        await app.dispose()


async def test_app_role_cannot_hijack_other_experts_published_pointer(pg: PgDb) -> None:
    """终审 F2（发布劫持）：own 上下文攻击者改写他人 experts.published_revision_id
    → UPDATE 的 USING 仅匹配 owner 本人，受害行 owner 不同 → 静默 0 行；
    superuser 复核发布指针原样（此前无 RLS 时该 UPDATE 会成功）。"""
    victim = await _seed_user_with_task(pg, "hijack-victim@x.com")
    attacker = await _seed_user_with_task(pg, "hijack-attacker@x.com")
    victim_expert, victim_published_rev = await _published_ids_for(pg, "experts", victim)
    _, attacker_rev = await _published_ids_for(pg, "experts", attacker)
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": attacker})
            updated = await conn.execute(
                text("UPDATE experts SET published_revision_id = :r WHERE id = :x"),
                {"r": attacker_rev, "x": victim_expert},
            )
            assert updated.rowcount == 0
    finally:
        await app.dispose()
    async with pg.engine.begin() as conn:  # superuser 复核：受害行发布指针未被改写
        current = (
            await conn.execute(
                text("SELECT published_revision_id FROM experts WHERE id = :x"),
                {"x": victim_expert},
            )
        ).scalar_one()
    assert current == victim_published_rev


async def test_app_role_cannot_delete_other_experts_row(pg: PgDb) -> None:
    """终审 F2（级联删除）：own 上下文攻击者 DELETE 他人 experts 行 → 0 行；
    否则 ON CASCADE 会连带删掉受害者的 expert_revisions。superuser 复核
    experts 行与 revision 均原样。"""
    victim = await _seed_user_with_task(pg, "del-victim@x.com")
    attacker = await _seed_user_with_task(pg, "del-attacker@x.com")
    victim_expert, _ = await _published_ids_for(pg, "experts", victim)
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": attacker})
            deleted = await conn.execute(
                text("DELETE FROM experts WHERE id = :x"), {"x": victim_expert}
            )
            assert deleted.rowcount == 0
    finally:
        await app.dispose()
    async with pg.engine.begin() as conn:  # superuser 复核：experts 行未被级联删除
        experts = (
            await conn.execute(
                text("SELECT count(*) FROM experts WHERE owner_id = :u"), {"u": victim}
            )
        ).scalar_one()
        assert experts == 1
        revisions = (
            await conn.execute(
                text("SELECT count(*) FROM expert_revisions WHERE owner_id = :u"),
                {"u": victim},
            )
        ).scalar_one()
        assert revisions == 1


async def test_app_role_stranger_and_guest_see_only_published_experts(pg: PgDb) -> None:
    """experts_app_select 发布可见性：陌生 owner 与无上下文 guest 都恰好看见
    已发布 expert；同作者的 draft expert 不泄漏（superuser 可见 2 条）。
    此前无 RLS 时陌生 owner 会看到全部 2 条。"""
    author = await _seed_user_with_task(pg, "pub-author@x.com")  # published expert
    async with pg.engine.begin() as conn:  # 同作者再加一条 draft expert
        await conn.execute(
            text(
                "INSERT INTO experts (id, owner_id, status) VALUES (gen_random_uuid(), :u, 'draft')"
            ),
            {"u": author},
        )
        total = (await conn.execute(text("SELECT count(*) FROM experts"))).scalar_one()
    assert total == 2  # draft 确实存在，只是被 RLS 过滤
    stranger = await _seed_bare_user(pg, "stranger2@x.com")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:  # 陌生 owner 上下文
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": stranger})
            n = (await conn.execute(text("SELECT count(*) FROM experts"))).scalar_one()
            assert n == 1
            status = (await conn.execute(text("SELECT status FROM experts"))).scalar_one()
            assert status == "published"
        async with app.connect() as conn:  # 全新连接：无 owner 上下文的 guest
            n = (await conn.execute(text("SELECT count(*) FROM experts"))).scalar_one()
            assert n == 1
    finally:
        await app.dispose()


async def test_app_role_cannot_tamper_other_skills_rows(pg: PgDb) -> None:
    """终审 F2 skills 镜像：own 上下文攻击者对他人 skills 行的 UPDATE（发布指针
    劫持）与 DELETE 均静默 0 行；superuser 复核原样。"""
    victim = await _seed_skill_with_revision(pg, "skill-victim@x.com")
    attacker = await _seed_skill_with_revision(pg, "skill-attacker@x.com")
    victim_skill, victim_published_rev = await _published_ids_for(pg, "skills", victim)
    _, attacker_rev = await _published_ids_for(pg, "skills", attacker)
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": attacker})
            updated = await conn.execute(
                text("UPDATE skills SET published_revision_id = :r WHERE id = :s"),
                {"r": attacker_rev, "s": victim_skill},
            )
            assert updated.rowcount == 0
            deleted = await conn.execute(
                text("DELETE FROM skills WHERE id = :s"), {"s": victim_skill}
            )
            assert deleted.rowcount == 0
    finally:
        await app.dispose()
    async with pg.engine.begin() as conn:  # superuser 复核：受害行原样
        current = (
            await conn.execute(
                text("SELECT published_revision_id FROM skills WHERE id = :s"),
                {"s": victim_skill},
            )
        ).scalar_one()
    assert current == victim_published_rev


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
    """policy 总数 ≥54（9 owner 表 ×5 + experts/skills ×5×2 + revision 表 ×3×2
    + reports ×3 = 64，0003 +7、0006 +7 → 实际 78）；admin 角色专属 policy
    ≥12（0006 后实际 23）；app 角色不持有任何 *_admin_* policy。"""
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
    """app role 表授权白名单：25 张 DML + 3 张只读 + 1 张限权写 = 29 张（0003：
    users 新增 SELECT/INSERT/UPDATE，user_entitlements 新增 SELECT，invitations
    补 UPDATE 转 DML；0009 起 admin 写授权矩阵只动 admin role 的 policy 面；
    0011 起 audit_logs 新增 INSERT 全表 + SELECT 限 created_at 单列——作者面
    offline/DELETE 的同事务审计（Sup §10.6「物理毁灭必须有审计痕」），整行审计
    读仍 app 不可见；0013 起 user_mcp_servers/user_mcp_tools 新增 DML 两张
    （Phase 10 M1 用户 MCP 面）；content_reviews/alembic_version 仍无授权。
    本守护即防漂移钉）。"""
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
    assert n == 31


async def test_owner_tables_have_rls_enabled_and_forced(pg: PgDb) -> None:
    """11 张 owner 表 relrowsecurity 与 relforcerowsecurity 双真：FORCE 使表 owner
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


async def test_users_rls_own_row_visible_and_update_allowed(pg):
    app = _role_engine(pg, APP_ROLE)
    try:
        uid = _uuid.uuid4()
        async with app.begin() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": str(uid)})
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (:i, 'a@x.test', 'h', 'user', 'pending')"
                ),
                {"i": str(uid)},
            )
            email = (
                await conn.execute(text("SELECT email FROM users WHERE id = :i"), {"i": str(uid)})
            ).scalar_one()
            assert email == "a@x.test"
            await conn.execute(
                text("UPDATE users SET status='active' WHERE id = :i"), {"i": str(uid)}
            )
    finally:
        await app.dispose()


async def test_users_rls_cross_owner_invisible(pg):
    """陌生 owner：SELECT 0 行、UPDATE 0 行（RLS 默认拒绝语义）。"""
    super_engine = pg.engine
    other = _uuid.uuid4()
    async with super_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status) "
                "VALUES (:i, 'b@x.test', 'h', 'user', 'pending')"
            ),
            {"i": str(other)},
        )
    app = _role_engine(pg, APP_ROLE)
    try:
        mine = _uuid.uuid4()
        async with app.begin() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": str(mine)})
            rows = (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one()
            assert rows == 0  # 看不到任何他人行
            result = await conn.execute(
                text("UPDATE users SET status='active' WHERE id = :i"), {"i": str(other)}
            )
            assert result.rowcount == 0
    finally:
        await app.dispose()


async def test_app_can_update_invitations_and_admin_update_outbox(pg):
    # 种子经 superuser：app 对 invitations 无 INSERT、admin 对 owner-RLS 表无 INSERT policy
    app = _role_engine(pg, APP_ROLE)
    admin = _role_engine(pg, ADMIN_ROLE)
    try:
        async with pg.engine.begin() as seed:
            uid = _uuid.uuid4()
            await seed.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (:i, 'd@x.test', 'h', 'user', 'active')"
                ),
                {"i": str(uid)},
            )
            await seed.execute(
                text(
                    "INSERT INTO invitations (id, token_hash, email, expires_at) "
                    "VALUES (:i, :t, 'c@x.test', now() + interval '1 day')"
                ),
                {"i": str(_uuid.uuid4()), "t": "f" * 64},
            )
            # attempts NOT NULL 无 server default
            await seed.execute(
                text(
                    "INSERT INTO email_outbox (id, user_id, purpose, payload_ciphertext, "
                    "state, attempts) "
                    "VALUES (:i, :u, 'email_verify', '{}', 'pending', 0)"
                ),
                {"i": str(_uuid.uuid4()), "u": str(uid)},
            )
        async with app.begin() as conn:
            result = await conn.execute(
                text("UPDATE invitations SET consumed_at = now() WHERE token_hash = :t"),
                {"t": "f" * 64},
            )
            assert result.rowcount == 1  # 0003 之前 42501
        async with admin.begin() as conn:
            result = await conn.execute(
                text("UPDATE email_outbox SET state='sent' WHERE state='pending'")
            )
            assert result.rowcount == 1  # 0003 之前静默 0 行
    finally:
        await app.dispose()
        await admin.dispose()
