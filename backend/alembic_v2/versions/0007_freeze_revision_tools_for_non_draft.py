"""revision_tools 冻结护栏（Phase 5 裁决 D7，认领 Phase 4 parked 项）。

revision_tools 是 approve 断言（review_service._assert_tools_enabled）与
Phase 5 扩展生成器的唯一工具事实源；0006 的 owner 护栏只覆盖四治理表，
revision_tools 的 app_all FOR ALL policy 允许 owner 对已提审/已发布 revision
的 tools 行任意增删改——审核通过的工具集与后续消费的工具集可分叉。
本迁移为 revision_tools 加 BEFORE INSERT/UPDATE/DELETE 触发器：owner 上下文
（GUC 已设）且父 revision status <> 'draft' → RAISE；admin/superuser 上下文
（GUC 未设）放行（kill switch 编排/测试种子不受限）。skill_revisions 无工具
绑定表，不涉及。

测试钉点：tests/test_v2_migrations.py head==0007；test_v2_rls.py 冻结行为测试。
asyncpg 单语句限制：每条 DDL 独立 op.execute。
"""

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402

_GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION app.guard_owner_revision_tools() RETURNS trigger AS $fn$
BEGIN
    IF app.current_owner_id() IS NULL THEN
        RETURN COALESCE(NEW, OLD);  -- admin/superuser 上下文：治理处置/种子路径，放行
    END IF;
    IF EXISTS (
        SELECT 1 FROM expert_revisions r
        WHERE r.id = COALESCE(NEW.expert_revision_id, OLD.expert_revision_id)
          AND r.status <> 'draft'
    ) THEN
        RAISE EXCEPTION '父 revision 非 draft，工具集已冻结';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$fn$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_GUARD_FUNCTION)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app.guard_owner_revision_tools() "
        "TO agentcraft_app, agentcraft_admin"
    )
    op.execute(
        "CREATE TRIGGER revision_tools_freeze_guard "
        "BEFORE INSERT OR UPDATE OR DELETE ON revision_tools "
        "FOR EACH ROW EXECUTE FUNCTION app.guard_owner_revision_tools()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS revision_tools_freeze_guard ON revision_tools")
    op.execute("DROP FUNCTION IF EXISTS app.guard_owner_revision_tools()")
