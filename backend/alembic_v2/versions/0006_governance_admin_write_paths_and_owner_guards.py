"""治理域 admin 写路径与 owner 上下文护栏（Phase 4 裁决 D1/D2/D3）。

1. admin UPDATE policy ×4：approve（CAS 发布）/reject/takedown 此前在 admin role
   下无适用 UPDATE policy → 静默 0 行（0001:1070-1071 注释预留）。范式沿用
   reports_admin_update（0001:1129-1131）。裁决 D1：approve 事务整体跑 admin 引擎，
   不引入 superuser/第三工厂。
2. revision_tools RLS：该表在 _APP_DML 白名单却无 RLS、无 owner 列 → 任意认证
   用户可跨作者读写删他人 revision_tools（approve 断言的输入可被篡改）。裁决 D2：
   ENABLE + 三支 policy——app FOR ALL（EXISTS 绑父 revision owner）+
   app published_read（FOR SELECT，EXISTS 父 revision 被 experts.published_revision_id
   指向——镜像 0001:1108-1113，缺它则匿名 discover 详情 tools 恒空）+ admin_read。
3. owner 上下文护栏（触发器，4 表共用函数，admin/superuser 上下文——GUC 未设——
   放行）：BEFORE UPDATE `guard_owner_content_writes`——app 上下文禁止改实体
   published_revision_id/status（自我发布封死）；revision 归属列冻结、status 仅
   允许 draft→pending_review、非 draft 内容冻结（D7 两段式语义的 DB 强制）。
   BEFORE INSERT `guard_owner_content_inserts`——app 上下文下内容只能生而为
   draft（封死「INSERT 生而为 pending_review/published」的审核队列投毒面）。
   注意 BEFORE ROW 触发器先于 RLS WITH CHECK 执行（owner_id 篡改由触发器
   P0001 拦截，WITH CHECK 42501 不可达）。裁决 D3。

测试钉点：tests/test_v2_migrations.py head==0006；test_v2_rls.py 追加 4 行为测试。
asyncpg 单语句限制：每条 DDL 独立 op.execute（0001:981-984 先例）。
"""

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402

_ADMIN_UPDATE_TABLES = ("experts", "skills", "expert_revisions", "skill_revisions")

_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION app.guard_owner_content_writes() RETURNS trigger AS $fn$
BEGIN
    IF app.current_owner_id() IS NULL THEN
        RETURN NEW;  -- admin/superuser 上下文：治理处置路径，放行
    END IF;
    IF TG_TABLE_NAME IN ('experts', 'skills') THEN
        IF NEW.published_revision_id IS DISTINCT FROM OLD.published_revision_id
           OR NEW.status IS DISTINCT FROM OLD.status THEN
            RAISE EXCEPTION 'owner 上下文禁止修改发布指针或实体状态';
        END IF;
    ELSE  -- expert_revisions / skill_revisions
        IF NEW.owner_id IS DISTINCT FROM OLD.owner_id
           OR NEW.revision_no IS DISTINCT FROM OLD.revision_no THEN
            RAISE EXCEPTION 'owner 上下文禁止修改 revision 归属或编号';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status
           AND NOT (OLD.status = 'draft' AND NEW.status = 'pending_review') THEN
            RAISE EXCEPTION 'owner 上下文仅允许 draft → pending_review';
        END IF;
        IF OLD.status <> 'draft'
           AND (NEW.content_json IS DISTINCT FROM OLD.content_json
                OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256) THEN
            RAISE EXCEPTION 'revision 提交后内容不可变';
        END IF;
    END IF;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;
"""

_INSERT_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION app.guard_owner_content_inserts() RETURNS trigger AS $fn$
BEGIN
    IF app.current_owner_id() IS NULL THEN
        RETURN NEW;  -- admin/superuser 上下文：治理处置/种子路径，放行
    END IF;
    IF NEW.status <> 'draft' THEN
        RAISE EXCEPTION 'owner 上下文下内容只能生而为 draft';
    END IF;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # ---- D1：admin UPDATE policy（RLS 下无适用 policy 的 UPDATE 静默 0 行）----
    for table in _ADMIN_UPDATE_TABLES:
        op.execute(
            f"CREATE POLICY {table}_admin_update ON {table} FOR UPDATE "
            f"TO agentcraft_admin USING (true)"
        )

    # ---- D2：revision_tools 行级隔离（EXISTS 绑父 revision owner）----
    op.execute("ALTER TABLE revision_tools ENABLE ROW LEVEL SECURITY;")
    op.execute(
        "CREATE POLICY revision_tools_app_all ON revision_tools FOR ALL "
        "TO agentcraft_app "
        "USING (EXISTS (SELECT 1 FROM expert_revisions r "
        "WHERE r.id = revision_tools.expert_revision_id "
        "AND r.owner_id = app.current_owner_id())) "
        "WITH CHECK (EXISTS (SELECT 1 FROM expert_revisions r "
        "WHERE r.id = revision_tools.expert_revision_id "
        "AND r.owner_id = app.current_owner_id()))"
    )
    op.execute(
        "CREATE POLICY revision_tools_app_published_read ON revision_tools "
        "FOR SELECT TO agentcraft_app "
        "USING (EXISTS (SELECT 1 FROM expert_revisions r "
        "WHERE r.id = revision_tools.expert_revision_id "
        "AND EXISTS (SELECT 1 FROM experts e "
        "WHERE e.published_revision_id = r.id)))"
    )
    op.execute(
        "CREATE POLICY revision_tools_admin_read ON revision_tools FOR SELECT "
        "TO agentcraft_admin USING (true)"
    )

    # ---- D3：owner 上下文护栏触发器（UPDATE + INSERT 两组）----
    op.execute(_GUARD_FUNCTION)
    op.execute(_INSERT_GUARD_FUNCTION)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.guard_owner_content_writes() "
        "TO agentcraft_app, agentcraft_admin"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.guard_owner_content_inserts() "
        "TO agentcraft_app, agentcraft_admin"
    )
    for table in _ADMIN_UPDATE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_owner_guard BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION app.guard_owner_content_writes()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_draft_birth_guard BEFORE INSERT ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION app.guard_owner_content_inserts()"
        )


def downgrade() -> None:
    for table in _ADMIN_UPDATE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_owner_guard ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_draft_birth_guard ON {table}")
    op.execute("DROP FUNCTION IF EXISTS app.guard_owner_content_writes()")
    op.execute("DROP FUNCTION IF EXISTS app.guard_owner_content_inserts()")
    op.execute("DROP POLICY IF EXISTS revision_tools_admin_read ON revision_tools")
    op.execute("DROP POLICY IF EXISTS revision_tools_app_published_read ON revision_tools")
    op.execute("DROP POLICY IF EXISTS revision_tools_app_all ON revision_tools")
    op.execute("ALTER TABLE revision_tools DISABLE ROW LEVEL SECURITY;")
    for table in _ADMIN_UPDATE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_admin_update ON {table}")
