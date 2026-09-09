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
  rowcount 断言）
- 未知角色无法连接（PUBLIC 表权限已全撤、角色白名单封闭）
- 目录契约（Task 6 评审补充）：policy 总数 ≥54（纳入 experts/skills 后实际 64）；
  admin 角色专属 policy ≥12（实际 15）且 app 角色不持有任何 *_admin_* policy；
  app 授权表白名单 26 张表；11 张 owner 表 relrowsecurity/relforcerowsecurity
  双真（FORCE：表 owner 亦受 RLS 约束）
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
    "experts",
    "skills",
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
    + reports ×3 = 64）；admin 角色专属 policy ≥12（实际 15）；
    app 角色不持有任何 *_admin_* policy。"""
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
