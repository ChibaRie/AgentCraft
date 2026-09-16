"""RLS 护栏（Phase 4 的 0006 / Phase 5 的 0007）：admin 写路径、revision_tools
跨 owner 隔离、owner 上下文护栏触发器、revision 不可变与 draft 出生约束。

Phase 9 T6 拆分自 test_v2_rls.py；共享助手见 v2_rls_helpers。
"""

import uuid as _uuid

import pytest
from sqlalchemy import text

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


async def _superuser_one(
    pg: PgDb, sql: str, params: dict[str, object] | None = None
) -> _uuid.UUID:  # 模块内小助手，放文件底部工具区
    async with pg.engine.begin() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


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
