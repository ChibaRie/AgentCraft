"""V2 RLS 强制执行测试（app/admin 双 role）— 越权矩阵的 DB 层证据。

针对 Task 6 迁移后的真实 schema（0001 roles+policies+RLS、0002 seeds，随模板库
克隆进入每个测试库）验证：

- app role 未设 owner 上下文 / 陌生 owner → SELECT 0 行，且 UPDATE/DELETE 同样
  走默认拒绝（静默 0 行受影响、数据原样保留——防 USING 被放宽为 true 的回归）
- expert_revisions 发布可见性（app_published_read）：陌生 owner 恰好看见已发布
  revision（EXISTS 经 experts.published_revision_id），draft 不泄漏
- app role 设 own owner → 可见；不改 owner_id 的 UPDATE 正常，改写 owner_id 被
  WITH CHECK 拒绝（抛错 + SQLSTATE 42501，而非 SELECT 不可见的静默 0 行）
- app role 无 DDL：CREATE TABLE 被权限拒绝（REVOKE CREATE ON SCHEMA public，
  SQLSTATE 42501）
- admin role 经显式 policy 读全量；无 UPDATE/DELETE policy → RLS 默认拒绝 =
  静默 0 行受影响（PostgreSQL 不抛错——brief 的 pytest.raises 版本据此修正为
  rowcount 断言）。0006 例外：experts/skills/expert_revisions/skill_revisions
  四张治理表新增 *_admin_update policy（Phase 4 裁决 D1），admin UPDATE 对这
  四张表自 0006 起生效，不再静默 0 行；其余表维持默认拒绝语义不变
- 未知角色无法连接（PUBLIC 表权限已全撤、角色白名单封闭）
- 目录契约（Task 6 评审补充）：policy 总数 ≥54（0006 后实际 78）；
  admin 角色专属 policy ≥12（实际 23）且 app 角色不持有任何 *_admin_* policy；
  app 授权表白名单 28 张表（0003 起纳入 users/user_entitlements）；11 张 owner 表
  relrowsecurity/relforcerowsecurity 双真（FORCE：表 owner 亦受 RLS 约束）
- experts/skills（终审 F2）：owner RLS + 发布可见性——SELECT 放行
  status='published'（陌生 owner / 无上下文 guest 可读公开目录）或 owner 本人；
  UPDATE/DELETE 仅 owner 本人 → 跨 owner 发布指针劫持（published_revision_id
  改写）与级联删除他人 revision 被静默 0 行拒绝

信任边界固化（方案 1 重定界）：app 凭据为受信后端秘密，RLS 不防凭据泄露/
注入；Phase 2 禁止拼接 SQL；nonce 表硬化为 V2.5+ 备选。文件末两条
test_boundary_* 用例断言的正是这一被接受的现实——自定义 GUC 可被 app role
直接伪造、app.set_current_owner 无授权校验——而非修复目标。

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

from tests.conftest import ADMIN_ROLE, APP_ROLE, PgDb, make_role_engine

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


def _role_engine(pg: PgDb, role: tuple[str, str]) -> AsyncEngine:
    """以指定角色（app/admin）建连接池，指向当前测试库。

    实现已收编为 tests.conftest.make_role_engine（Phase 6 T2 role_engine 工厂
    夹具的底层）；本模块级别名保留，既有调用点零改动。"""
    return make_role_engine(pg, role)


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


async def _seed_bare_user(pg: PgDb, email: str) -> _uuid.UUID:
    """superuser 造一个无任何数据行的用户（充当陌生 owner 的真实 user id）。"""
    async with pg.engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), :e, 'h', 'user', 'active') RETURNING id"
                ),
                {"e": email},
            )
        ).scalar_one()


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


async def _published_ids_for(
    pg: PgDb, table: str, owner: _uuid.UUID
) -> tuple[_uuid.UUID, _uuid.UUID]:
    """superuser 查指定 owner 在 experts/skills 表的 (行 id, published_revision_id)。"""
    assert table in ("experts", "skills")
    async with pg.engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    f"SELECT id, published_revision_id FROM {table} WHERE owner_id = :u ORDER BY id"
                ),
                {"u": owner},
            )
        ).one()
    return row[0], row[1]


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
    """app role 表授权白名单：25 张 DML + 3 张只读 = 28 张（0003：users 新增
    SELECT/INSERT/UPDATE，user_entitlements 新增 SELECT，invitations 补 UPDATE 转 DML；
    content_reviews/audit_logs/alembic_version 仍无授权）。"""
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
    assert n == 28


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


# ---------- 信任边界固化（方案 1 重定界，终审 F1）----------
# 以下两条断言的是被接受的现实，而非修复目标：自定义 GUC 无 ACL，app role
# 可绕过 app.set_current_owner 直接伪造 owner 上下文；set_current_owner 本身
# 亦无授权校验。方案 1 裁决：app 数据库凭据是受信后端秘密，RLS 防的是
# 应用层 owner 上下文遗漏 bug，不防凭据泄露/注入。Phase 2 禁止拼接 SQL；
# nonce 表硬化为 V2.5+ 备选。若未来这两条用例转红，说明角色/GUC ACL 语义
# 发生变化，需重新评估信任边界而非简单改断言。


async def test_boundary_guc_can_be_forged_without_set_current_owner(pg: PgDb) -> None:
    """边界（a）：app role 直接 SELECT set_config('app.current_owner_id', <victim>,
    true)（不经 app.set_current_owner）→ current_owner_id() 返回受害者 id →
    受害者任务可见。固化信任边界：自定义 GUC 可伪造是 PostgreSQL 既有语义。"""
    victim = await _seed_user_with_task(pg, "forge-victim@x.com")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            # 显式事务内执行：set_config 的 is_local=true 随事务结束回滚，
            # SQLAlchemy autobegin 保证三条语句同处一个事务（与真实攻击形态一致）
            await conn.execute(
                text("SELECT set_config('app.current_owner_id', :v, true)"),
                {"v": str(victim)},
            )
            forged = (await conn.execute(text("SELECT app.current_owner_id()"))).scalar_one()
            assert forged == victim
            n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
            assert n == 1  # 伪造上下文下受害者任务可见
    finally:
        await app.dispose()


async def test_boundary_set_current_owner_performs_no_authorization(pg: PgDb) -> None:
    """边界（b）：app.set_current_owner 对任意 uuid 不做授权校验——传受害者 id
    即获得其数据视野。固化信任边界：该函数仅是 GUC 写入器，授权在应用层
    （owner 上下文只能由服务端会话派生）。"""
    victim = await _seed_user_with_task(pg, "fn-victim@x.com")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": victim})
            forged = (await conn.execute(text("SELECT app.current_owner_id()"))).scalar_one()
            assert forged == victim
            n = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
            assert n == 1
    finally:
        await app.dispose()


# ---- Phase 4（0006）：admin 写路径 / revision_tools 隔离 / owner 护栏 ----


async def _seed_revision_tools(pg: PgDb, owner: _uuid.UUID) -> _uuid.UUID:
    """superuser 造独立链：draft expert（无 published 指针）→ revision_no=1
    （draft——0007 冻结护栏起，owner 正向插行要求父 revision 尚为 draft，
    提审后工具集冻结）→ revision_tools 一行，返回 revision id。

    刻意不挂 published_revision_id 指针：stranger 对 revision_tools 的 0 行断言
    不得被 revision_tools_app_published_read（0006 的指针读通路）命中；也避开
    _seed_user_with_task 已占用的 revision_no=1（UNIQUE(expert_id, revision_no)）。
    """
    async with pg.engine.begin() as conn:
        expert_id = (
            await conn.execute(
                text(
                    "INSERT INTO experts (id, owner_id, status) "
                    "VALUES (gen_random_uuid(), :u, 'draft') RETURNING id"
                ),
                {"u": owner},
            )
        ).scalar_one()
        revision_id = (
            await conn.execute(
                text(
                    "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) VALUES (gen_random_uuid(), :x, "
                    ":u, 1, '{}', :h, 'draft') RETURNING id"
                ),
                {"x": expert_id, "u": owner, "h": "d" * 64},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO revision_tools (id, expert_revision_id, tool_id, version) "
                "VALUES (gen_random_uuid(), :r, 'check_code_style', '1')"
            ),
            {"r": revision_id},
        )
    return revision_id


async def test_admin_role_updates_governance_rows_after_0006(pg: PgDb) -> None:
    """0006 admin UPDATE policy 生效：admin role 可翻转 experts.status 与
    expert_revisions.status（此前无适用 policy → 静默 0 行——T5 approve 的前提）。"""
    await _seed_user_with_task(pg, "admin-writer@x.com")
    expert_id = await _superuser_one(pg, "SELECT id FROM experts LIMIT 1")
    revision_id = await _superuser_one(pg, "SELECT id FROM expert_revisions LIMIT 1")
    admin = _role_engine(pg, ADMIN_ROLE)
    try:
        async with admin.connect() as conn:
            updated = await conn.execute(
                text("UPDATE experts SET status = 'published' WHERE id = :x"),
                {"x": expert_id},
            )
            assert updated.rowcount == 1
            updated = await conn.execute(
                text("UPDATE expert_revisions SET status = 'archived' WHERE id = :r"),
                {"r": revision_id},
            )
            assert updated.rowcount == 1
    finally:
        await admin.dispose()


async def _superuser_one(
    pg: PgDb, sql: str, params: dict[str, object] | None = None
) -> _uuid.UUID:  # 模块内小助手，放文件底部工具区
    async with pg.engine.begin() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def test_revision_tools_cross_owner_isolation(pg: PgDb) -> None:
    """0006 revision_tools RLS：owner 上下文可见自己的行、可对自己 revision 插行
    （submit 落行的正向通路——0007 起该通路即 draft 期写行，父 revision 取 draft）；
    陌生 owner 上下文 0 行（published 指针通路不命中）、
    对他人 revision 插行被 WITH CHECK 拒绝（42501）；admin 全量可读。
    每个上下文独立连接块：同连接的 autobegin 事务共享且 GUC 事务本地，一条语句
    失败会 abort 该事务内全部后续语句（InFailedSqlTransaction 25P02）。"""
    owner = await _seed_bare_user(pg, "tools-owner@x.com")
    revision_id = await _seed_revision_tools(pg, owner)
    stranger = await _seed_bare_user(pg, "tools-stranger@x.com")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.begin() as conn:  # owner 正向：可见 + 可对自己 revision 插行（begin 持久化）
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            n = (await conn.execute(text("SELECT count(*) FROM revision_tools"))).scalar_one()
            assert n == 1
            await conn.execute(
                text(
                    "INSERT INTO revision_tools (id, expert_revision_id, tool_id, version) "
                    "VALUES (gen_random_uuid(), :r, 'read_task_file', '1')"
                ),
                {"r": revision_id},
            )
        async with app.connect() as conn:  # stranger：0 行（owner 隔离 + 指针通路不命中）
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": stranger})
            n = (await conn.execute(text("SELECT count(*) FROM revision_tools"))).scalar_one()
            assert n == 0
        async with app.connect() as conn:  # stranger 越权 INSERT：WITH CHECK 拒绝（单块单失败）
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": stranger})
            with pytest.raises(Exception, match="row-level security policy") as excinfo:
                await conn.execute(
                    text(
                        "INSERT INTO revision_tools (id, expert_revision_id, tool_id, version) "
                        "VALUES (gen_random_uuid(), :r, 'list_task_files', '1')"
                    ),
                    {"r": revision_id},
                )
            assert excinfo.value.orig.sqlstate == "42501"
    finally:
        await app.dispose()
    admin = _role_engine(pg, ADMIN_ROLE)
    try:
        async with admin.connect() as conn:
            n = (await conn.execute(text("SELECT count(*) FROM revision_tools"))).scalar_one()
            assert n == 2  # 种子行 + owner 正向插入行
    finally:
        await admin.dispose()


async def test_owner_context_cannot_flip_entity_status_or_pointer(pg: PgDb) -> None:
    """0006 护栏触发器：owner 上下文改自己实体的 published_revision_id/status
    → 触发器抛错（自我发布面封死）。admin 上下文（无 GUC）放行。
    每个预期失败语句独立连接块（单块单失败——同块事务会 abort）。"""
    owner = await _seed_user_with_task(pg, "selfpub@x.com")
    expert_id, _ = await _published_ids_for(pg, "experts", owner)
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="禁止修改发布指针或实体状态"):
                await conn.execute(
                    text("UPDATE experts SET published_revision_id = NULL WHERE id = :x"),
                    {"x": expert_id},
                )
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="禁止修改发布指针或实体状态"):
                await conn.execute(
                    text("UPDATE experts SET status = 'archived' WHERE id = :x"),
                    {"x": expert_id},
                )
    finally:
        await app.dispose()
    admin = _role_engine(pg, ADMIN_ROLE)
    try:
        async with admin.connect() as conn:  # admin 上下文（无 GUC）不受护栏约束
            updated = await conn.execute(
                text("UPDATE experts SET status = 'draft' WHERE id = :x"), {"x": expert_id}
            )
            assert updated.rowcount == 1
    finally:
        await admin.dispose()


async def test_revision_immutability_guard(pg: PgDb) -> None:
    """0006 revision 护栏：draft 内容可改 + draft→pending_review 放行；
    非 draft 内容冻结；status 回退/跳变拒绝；owner_id 改动被 0006 护栏触发器
    拒绝（BEFORE ROW 触发器先于 RLS WITH CHECK 执行 → P0001 触发器消息，
    WITH CHECK 的 42501 在此路径不可达）。每个预期失败语句独立连接块。"""
    owner = await _seed_user_with_task(pg, "immutable@x.com")
    expert_id = await _superuser_one(pg, "SELECT id FROM experts LIMIT 1")
    async with pg.engine.begin() as conn:  # 造一条 draft revision（no=2，避开已占的 no=1）
        draft_id = (
            await conn.execute(
                text(
                    "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) VALUES (gen_random_uuid(), :x, "
                    ":u, 2, '{}', :h, 'draft') RETURNING id"
                ),
                {"x": expert_id, "u": owner, "h": "e" * 64},
            )
        ).scalar_one()
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.begin() as conn:  # 放行块：draft 覆写内容 + 提审流转（begin：跨块持久化）
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            await conn.execute(
                text("UPDATE expert_revisions SET content_json = '{\"a\"\\:1}' WHERE id = :r"),
                {"r": draft_id},
            )
            await conn.execute(
                text("UPDATE expert_revisions SET status = 'pending_review' WHERE id = :r"),
                {"r": draft_id},
            )
        async with app.connect() as conn:  # 负例 1：非 draft 内容冻结
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="revision 提交后内容不可变"):
                await conn.execute(
                    text("UPDATE expert_revisions SET content_json = '{}' WHERE id = :r"),
                    {"r": draft_id},
                )
        async with app.connect() as conn:  # 负例 2：status 回退拒绝
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="仅允许 draft"):
                await conn.execute(
                    text("UPDATE expert_revisions SET status = 'draft' WHERE id = :r"),
                    {"r": draft_id},
                )
        async with app.connect() as conn:  # 负例 3：owner_id 改动 → 触发器先行拦截
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="禁止修改 revision 归属或编号") as excinfo:
                await conn.execute(
                    text("UPDATE expert_revisions SET owner_id = gen_random_uuid() WHERE id = :r"),
                    {"r": draft_id},
                )
            assert excinfo.value.orig.sqlstate == "P0001"
    finally:
        await app.dispose()
    admin = _role_engine(pg, ADMIN_ROLE)
    try:
        async with admin.connect() as conn:  # admin 上下文（无 GUC）不受护栏约束
            updated = await conn.execute(
                text(
                    "UPDATE expert_revisions SET content_json = '{\"admin\"\\:true}' WHERE id = :r"
                ),
                {"r": draft_id},
            )
            assert updated.rowcount == 1
    finally:
        await admin.dispose()


async def test_insert_guard_requires_draft_birth(pg: PgDb) -> None:
    """0006 INSERT 护栏：owner 上下文下 revision 只能生而为 draft（封死
    「INSERT 生而为 pending_review/published」的审核队列投毒面）；admin 上下文
    不受限（测试种子即 admin/superuser 形态）。单块单失败。"""
    owner = await _seed_user_with_task(pg, "birthguard@x.com")
    expert_id = await _superuser_one(pg, "SELECT id FROM experts LIMIT 1")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:  # owner 正向：draft 直插放行
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            await conn.execute(
                text(
                    "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) VALUES (gen_random_uuid(), :x, "
                    ":u, 3, '{}', :h, 'draft')"
                ),
                {"x": expert_id, "u": owner, "h": "f" * 64},
            )
        async with app.connect() as conn:  # 负例：生而为 pending_review
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="只能生而为 draft"):
                await conn.execute(
                    text(
                        "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                        "content_json, content_sha256, status) VALUES (gen_random_uuid(), :x, "
                        ":u, 4, '{}', :h, 'pending_review')"
                    ),
                    {"x": expert_id, "u": owner, "h": "9" * 64},
                )
    finally:
        await app.dispose()


async def test_app_role_for_update_excludes_foreign_published_entity(pg: PgDb) -> None:
    """跨 owner 锁语义钉（终审 F1）：同一 stranger 上下文，普通 SELECT 恰见他人
    published 实体（experts_app_select 发布可见性），SELECT ... FOR UPDATE 却
    0 行——experts_app_update 的 USING(owner_id = current_owner_id()) 不匹配的
    行在锁请求时被静默排除。这正是 edit_entity/submit_revision 跨 owner 统一
    404 的机制地基（author_service 实体行锁 scalar_one_or_none() → 404
    NOT_FOUND）：若服务层去掉 with_for_update()，锁查询退化为可见查询，跨
    owner 注入通道重开。本用例无预期失败语句，单连接块即可（B 纪律）。"""
    victim = await _seed_user_with_task(pg, "lock-victim@x.com")
    victim_expert, _ = await _published_ids_for(pg, "experts", victim)
    stranger = await _seed_bare_user(pg, "lock-stranger@x.com")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": stranger})
            visible = (
                await conn.execute(
                    text("SELECT id FROM experts WHERE id = :x"), {"x": victim_expert}
                )
            ).all()
            assert len(visible) == 1  # 发布可见性：普通 SELECT 恰见该行
            locked = (
                await conn.execute(
                    text("SELECT id FROM experts WHERE id = :x FOR UPDATE"),
                    {"x": victim_expert},
                )
            ).all()
            assert locked == []  # FOR UPDATE：UPDATE policy USING 不匹配 → 静默排除 0 行
    finally:
        await app.dispose()


# ---- Phase 5（0007）：revision_tools 冻结护栏 ----


async def _seed_tools_with_revision_states(pg: PgDb) -> tuple[_uuid.UUID, _uuid.UUID]:
    """superuser 造一个作者的两条 revision（draft no=1 / pending_review no=2），
    各带一行 revision_tools，返回 (draft_tools_row_id, pending_tools_row_id)。"""
    async with pg.engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), 'tools-freeze@x.com', 'h', 'user', 'active') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        expert_id = (
            await conn.execute(
                text(
                    "INSERT INTO experts (id, owner_id, status) "
                    "VALUES (gen_random_uuid(), :u, 'draft') RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
        out = []
        for no, status in ((1, "draft"), (2, "pending_review")):
            revision_id = (
                await conn.execute(
                    text(
                        "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                        "content_json, content_sha256, status) VALUES (gen_random_uuid(), :x, "
                        ":u, :n, '{}', :h, :s) RETURNING id"
                    ),
                    {"x": expert_id, "u": user_id, "n": no, "h": str(no) * 64, "s": status},
                )
            ).scalar_one()
            tools_row = (
                await conn.execute(
                    text(
                        "INSERT INTO revision_tools (id, expert_revision_id, tool_id, version) "
                        "VALUES (gen_random_uuid(), :r, 'check_code_style', '1') RETURNING id"
                    ),
                    {"r": revision_id},
                )
            ).scalar_one()
            out.append(tools_row)
    return out[0], out[1]


async def test_revision_tools_frozen_once_parent_not_draft(pg: PgDb) -> None:
    """0007 冻结护栏：owner 上下文对 draft 父行的 tools 行可改/可删（提审前可调整），
    对非 draft 父行 INSERT/UPDATE/DELETE 全部 RAISE；admin 上下文放行。
    每个预期失败语句独立连接块（单块单失败纪律）。"""
    draft_row, pending_row = await _seed_tools_with_revision_states(pg)
    owner = await _superuser_one(pg, "SELECT owner_id FROM experts LIMIT 1")
    app = _role_engine(pg, APP_ROLE)
    try:
        async with app.connect() as conn:  # draft 父行：正向放行（UPDATE 再改回）
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            await conn.execute(
                text("UPDATE revision_tools SET version = '1' WHERE id = CAST(:r AS uuid)"),
                {"r": draft_row},
            )
        async with app.connect() as conn:  # 非 draft 父行：UPDATE 拒绝
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="父 revision 非 draft"):
                await conn.execute(
                    text(
                        "UPDATE revision_tools SET tool_id = 'read_task_file' "
                        "WHERE id = CAST(:r AS uuid)"
                    ),
                    {"r": pending_row},
                )
        async with app.connect() as conn:  # 非 draft 父行：DELETE 拒绝
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            with pytest.raises(Exception, match="父 revision 非 draft"):
                await conn.execute(
                    text("DELETE FROM revision_tools WHERE id = CAST(:r AS uuid)"),
                    {"r": pending_row},
                )
        async with app.connect() as conn:  # 非 draft 父行：INSERT 拒绝
            await conn.execute(text("SELECT app.set_current_owner(:u)"), {"u": owner})
            parent = await _superuser_one(
                pg,
                "SELECT expert_revision_id FROM revision_tools "
                "WHERE id = CAST(:r AS uuid)",  # 取父 revision id 需 superuser
                {"r": pending_row},
            )
            with pytest.raises(Exception, match="父 revision 非 draft"):
                await conn.execute(
                    text(
                        "INSERT INTO revision_tools (id, expert_revision_id, tool_id, version) "
                        "VALUES (gen_random_uuid(), CAST(:r AS uuid), 'list_task_files', '1')"
                    ),
                    {"r": parent},
                )
    finally:
        await app.dispose()
    admin = _role_engine(pg, ADMIN_ROLE)
    try:
        async with admin.connect() as conn:
            # RLS 默认拒绝：admin 对 revision_tools 仅有 admin_read policy（0006:108-111），
            # 无 UPDATE policy → 静默 0 行（触发器放行救不了 RLS 行过滤——两层独立机制）
            updated = await conn.execute(
                text("UPDATE revision_tools SET version = '1' WHERE id = CAST(:r AS uuid)"),
                {"r": pending_row},
            )
            assert updated.rowcount == 0
    finally:
        await admin.dispose()
    async with pg.engine.begin() as conn:  # superuser（GUC 未设、不受 RLS 限）：钉触发器放行语义
        updated = await conn.execute(
            text("UPDATE revision_tools SET version = '1' WHERE id = CAST(:r AS uuid)"),
            {"r": pending_row},
        )
        assert updated.rowcount == 1


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
