"""identity domain grants and rls

Revision ID: 0003
Revises: 0002

Phase 2 授权修补（recon 实机验证的四处阻断）+ Phase 2 列修订：
1. app role 此前对 users/user_entitlements 零授权（42501）→ 授予并**同时加 RLS**
   （拒绝裸授权：users 按 id、user_entitlements 按 user_id 限本行，admin_read 全量）。
2. invitations app role 仅 SELECT（DB §3.1）→ 补 UPDATE（accept 消费语义，服务层
   保证仅 consumed_at/revoked_at 类状态列被写）。
3. email_outbox admin role 无 UPDATE policy（dispatcher 静默 0 行）→ 补
   email_outbox_admin_update（模板同 reports_admin_update 0001:1129-1131）。
4. users.deletion_deadline_at（注销剩余天数）+ sessions.device_label（设备摘要）。
downgrade 逆向：先删列/约束，再删策略，最后 REVOKE。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("deletion_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_users_deletion_deadline_consistency",
        "users",
        "deletion_deadline_at IS NULL OR status = 'deleting'",
    )
    op.add_column("sessions", sa.Column("device_label", sa.String(length=200), nullable=True))

    op.execute("GRANT SELECT, INSERT, UPDATE ON users TO agentcraft_app")
    op.execute("GRANT SELECT ON user_entitlements TO agentcraft_app")
    op.execute("GRANT UPDATE ON invitations TO agentcraft_app")

    # users：owner = 行本身（id = current_owner_id()）
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY;")
    op.execute(
        "CREATE POLICY users_app_select ON users FOR SELECT TO agentcraft_app "
        "USING (id = app.current_owner_id());"
    )
    op.execute(
        "CREATE POLICY users_app_insert ON users FOR INSERT TO agentcraft_app "
        "WITH CHECK (id = app.current_owner_id());"
    )
    op.execute(
        "CREATE POLICY users_app_update ON users FOR UPDATE TO agentcraft_app "
        "USING (id = app.current_owner_id()) WITH CHECK (id = app.current_owner_id());"
    )
    op.execute(
        "CREATE POLICY users_admin_read ON users FOR SELECT TO agentcraft_admin USING (true);"
    )

    # user_entitlements：按 user_id 限本行（Phase 2 仅 SELECT；授予/撤销属 Phase 7 admin 面，
    # 届时补 admin INSERT/UPDATE policy）
    op.execute("ALTER TABLE user_entitlements ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE user_entitlements FORCE ROW LEVEL SECURITY;")
    op.execute(
        "CREATE POLICY user_entitlements_app_select ON user_entitlements FOR SELECT "
        "TO agentcraft_app USING (user_id = app.current_owner_id());"
    )
    op.execute(
        "CREATE POLICY user_entitlements_admin_read ON user_entitlements FOR SELECT "
        "TO agentcraft_admin USING (true);"
    )
    op.execute(
        "CREATE POLICY email_outbox_admin_update ON email_outbox FOR UPDATE "
        "TO agentcraft_admin USING (true);"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS email_outbox_admin_update ON email_outbox")
    op.execute("DROP POLICY IF EXISTS user_entitlements_admin_read ON user_entitlements")
    op.execute("DROP POLICY IF EXISTS user_entitlements_app_select ON user_entitlements")
    op.execute("ALTER TABLE user_entitlements DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE user_entitlements NO FORCE ROW LEVEL SECURITY;")
    op.execute("DROP POLICY IF EXISTS users_admin_read ON users")
    op.execute("DROP POLICY IF EXISTS users_app_update ON users")
    op.execute("DROP POLICY IF EXISTS users_app_insert ON users")
    op.execute("DROP POLICY IF EXISTS users_app_select ON users")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY;")
    # 含 SELECT：0003 upgrade 授了三项，回到 0001 的零授权态
    op.execute("REVOKE SELECT, INSERT, UPDATE ON users FROM agentcraft_app")
    op.drop_column("sessions", "device_label")
    op.drop_constraint("ck_users_deletion_deadline_consistency", "users", type_="check")
    op.drop_column("users", "deletion_deadline_at")
