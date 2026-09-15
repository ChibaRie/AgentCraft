"""Phase 7 admin 写授权矩阵（裁决 D4 五项）+ 0003 预留兑现。

1. users_admin_update FOR UPDATE policy（USING (true) WITH CHECK (true)）——
   admin 用户管理仅服务 status 翻转（suspend/unsuspend，Phase 7 T3b C4 单语句；
   quotas 走 user_quotas 无 RLS 表，不需要本 policy）；
2. user_entitlements admin INSERT/UPDATE/DELETE 三支 policy——0003:63-64 注释
   预留（「授予/撤销属 Phase 7 admin 面，届时补 admin INSERT/UPDATE policy」）
   的兑现（grant/revoke，Phase 7 T3a）；
3. email_outbox_admin_insert FOR INSERT TO admin WITH CHECK (true)——**必须**：
   邀请场景 outbox 行 user_id=NULL，app 插入路径被 email_outbox_app_insert 的
   WITH CHECK (user_id = current_owner_id()) 堵死（NULL 不等于任何 owner），admin
   事务内 enqueue（T2 邀请邮件与邀请行同事务落库）否则 42501；
4. provider_catalog / tool_catalog / idempotency_records 零动作——admin blanket
   ALL 授权（0001:1025）已覆盖：目录表无 RLS、幂等表无 owner 语义，勿加；
5. downgrade 逆序（先插后改先改后翻的镜像序）。

不加 sessions/account_action_tokens admin 写 policy（D16 两段式 owner_session
承载——admin role 对 owner-RLS 表维持仅 *_admin_read）；不加 banned 值；不动
owner 面 policy。
asyncpg 单语句限制：每条 DDL 独立 op.execute（0001 先例）。
测试钉点：tests/test_v2_migrations.py head==0009；tests/test_v2_rls.py 行为断言
（admin UPDATE users 翻 status 行值真实变化、entitlement INSERT/UPDATE/DELETE、
email_outbox user_id=NULL 插入 + app 插入 42501 负例）。
"""

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402


def upgrade() -> None:
    # ---- (1) users：admin 仅 status 翻转（suspend/unsuspend）----
    op.execute(
        "CREATE POLICY users_admin_update ON users FOR UPDATE "
        "TO agentcraft_admin USING (true) WITH CHECK (true)"
    )
    # ---- (2) user_entitlements：授予/撤销（0003:63-64 预留兑现）----
    op.execute(
        "CREATE POLICY user_entitlements_admin_insert ON user_entitlements FOR INSERT "
        "TO agentcraft_admin WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY user_entitlements_admin_update ON user_entitlements FOR UPDATE "
        "TO agentcraft_admin USING (true) WITH CHECK (true)"
    )
    op.execute(
        "CREATE POLICY user_entitlements_admin_delete ON user_entitlements FOR DELETE "
        "TO agentcraft_admin USING (true)"
    )
    # ---- (3) email_outbox：admin enqueue（邀请场景 user_id=NULL）----
    op.execute(
        "CREATE POLICY email_outbox_admin_insert ON email_outbox FOR INSERT "
        "TO agentcraft_admin WITH CHECK (true)"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS email_outbox_admin_insert ON email_outbox")
    op.execute("DROP POLICY IF EXISTS user_entitlements_admin_delete ON user_entitlements")
    op.execute("DROP POLICY IF EXISTS user_entitlements_admin_update ON user_entitlements")
    op.execute("DROP POLICY IF EXISTS user_entitlements_admin_insert ON user_entitlements")
    op.execute("DROP POLICY IF EXISTS users_admin_update ON users")
