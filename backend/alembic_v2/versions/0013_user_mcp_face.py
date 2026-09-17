"""用户 MCP 面两表（Phase 10 M1；2026-09-17 受控逆转立项）。

- user_mcp_servers：用户自注册 MCP server（stdio 命令面信封密文 / http 地址面），
  owner-RLS（SELECT/UPDATE/DELETE owner 门 + INSERT WITH CHECK owner）+ admin 仅
  SELECT（D4 矩阵惯例，0001 owner_tables 同款循环）；
- user_mcp_tools：discover 的工具描述符缓存（server_id FK CASCADE，先删后插幂等），
  owner-RLS 经 EXISTS(user_mcp_servers) 子查询判定（expert_revisions published_read
  同族），admin 仅 SELECT；tool_name 与 transport 形态约束为模型/迁移双保险。

授权：app role 全 DML 两表；admin role 仅 SELECT（配合 *_admin_read policy）；
policy 随 DROP TABLE 清除，无显式 DROP。asyncpg 单语句限制：每条 DDL 独立
op.execute。

测试钉点：tests/test_v2_mcp.py（0013 往返 + 结构断言 + CRUD/发现链）；
test_v2_migrations.py head==0013；test_v2_rls.py app 授权白名单 31 张。

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_mcp_servers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("transport_kind", sa.String(length=10), nullable=False),
        sa.Column("command_encrypted", sa.Text(), nullable=True),
        sa.Column("command_dek_wrapped", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "transport_kind IN ('stdio', 'http')",
            name=op.f("ck_user_mcp_servers_transport_kind_enum"),
        ),
        sa.CheckConstraint(
            "(transport_kind = 'stdio' AND command_encrypted IS NOT NULL AND url IS NULL) "
            "OR (transport_kind = 'http' AND command_encrypted IS NULL AND url IS NOT NULL)",
            name=op.f("ck_user_mcp_servers_transport_payload"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_user_mcp_servers_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_mcp_servers")),
    )
    op.create_index(
        op.f("ix_user_mcp_servers_owner_id"), "user_mcp_servers", ["owner_id"], unique=False
    )
    op.create_table(
        "user_mcp_tools",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("schema_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["user_mcp_servers.id"],
            name=op.f("fk_user_mcp_tools_server_id_user_mcp_servers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_mcp_tools")),
        sa.UniqueConstraint("server_id", "tool_name", name="uq_user_mcp_tools_server_tool"),
    )
    op.create_index(
        op.f("ix_user_mcp_tools_server_id"), "user_mcp_tools", ["server_id"], unique=False
    )

    # ---- 授权（D4 矩阵：app 全 DML；admin 仅 SELECT 配合 admin_read policy）----
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON user_mcp_servers TO agentcraft_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON user_mcp_tools TO agentcraft_app")
    op.execute("GRANT SELECT ON user_mcp_servers TO agentcraft_admin")
    op.execute("GRANT SELECT ON user_mcp_tools TO agentcraft_admin")

    # ---- owner RLS：user_mcp_servers（0001 owner_tables 循环同款）----
    op.execute("ALTER TABLE user_mcp_servers ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE user_mcp_servers FORCE ROW LEVEL SECURITY;")
    op.execute(
        "CREATE POLICY user_mcp_servers_app_select ON user_mcp_servers FOR SELECT "
        "TO agentcraft_app USING (owner_id = app.current_owner_id());"
    )
    op.execute(
        "CREATE POLICY user_mcp_servers_app_insert ON user_mcp_servers FOR INSERT "
        "TO agentcraft_app WITH CHECK (owner_id = app.current_owner_id());"
    )
    op.execute(
        "CREATE POLICY user_mcp_servers_app_update ON user_mcp_servers FOR UPDATE "
        "TO agentcraft_app USING (owner_id = app.current_owner_id()) "
        "WITH CHECK (owner_id = app.current_owner_id());"
    )
    op.execute(
        "CREATE POLICY user_mcp_servers_app_delete ON user_mcp_servers FOR DELETE "
        "TO agentcraft_app USING (owner_id = app.current_owner_id());"
    )
    op.execute(
        "CREATE POLICY user_mcp_servers_admin_read ON user_mcp_servers FOR SELECT "
        "TO agentcraft_admin USING (true);"
    )

    # ---- owner RLS：user_mcp_tools（EXISTS 子查询判定 owner，无冗余 owner_id 列）----
    op.execute("ALTER TABLE user_mcp_tools ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE user_mcp_tools FORCE ROW LEVEL SECURITY;")
    op.execute(
        "CREATE POLICY user_mcp_tools_app_select ON user_mcp_tools FOR SELECT "
        "TO agentcraft_app USING (EXISTS (SELECT 1 FROM user_mcp_servers s "
        "WHERE s.id = user_mcp_tools.server_id "
        "AND s.owner_id = app.current_owner_id()));"
    )
    op.execute(
        "CREATE POLICY user_mcp_tools_app_insert ON user_mcp_tools FOR INSERT "
        "TO agentcraft_app WITH CHECK (EXISTS (SELECT 1 FROM user_mcp_servers s "
        "WHERE s.id = user_mcp_tools.server_id "
        "AND s.owner_id = app.current_owner_id()));"
    )
    op.execute(
        "CREATE POLICY user_mcp_tools_app_update ON user_mcp_tools FOR UPDATE "
        "TO agentcraft_app USING (EXISTS (SELECT 1 FROM user_mcp_servers s "
        "WHERE s.id = user_mcp_tools.server_id AND s.owner_id = app.current_owner_id())) "
        "WITH CHECK (EXISTS (SELECT 1 FROM user_mcp_servers s "
        "WHERE s.id = user_mcp_tools.server_id AND s.owner_id = app.current_owner_id()));"
    )
    op.execute(
        "CREATE POLICY user_mcp_tools_app_delete ON user_mcp_tools FOR DELETE "
        "TO agentcraft_app USING (EXISTS (SELECT 1 FROM user_mcp_servers s "
        "WHERE s.id = user_mcp_tools.server_id AND s.owner_id = app.current_owner_id()));"
    )
    op.execute(
        "CREATE POLICY user_mcp_tools_admin_read ON user_mcp_tools FOR SELECT "
        "TO agentcraft_admin USING (true);"
    )


def downgrade() -> None:
    # policy 随 DROP TABLE 一并清除（0001 惯例）
    op.drop_index(op.f("ix_user_mcp_tools_server_id"), table_name="user_mcp_tools")
    op.drop_table("user_mcp_tools")
    op.drop_index(op.f("ix_user_mcp_servers_owner_id"), table_name="user_mcp_servers")
    op.drop_table("user_mcp_servers")
