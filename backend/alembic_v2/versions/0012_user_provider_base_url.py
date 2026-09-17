"""user_providers 去目录化（2026-09-17 用户裁决：用户自带 base_url + Key + 模型）。

- 新增 base_url（512，OpenAI 兼容上游地址；存量行从 catalog allowed_host+path_prefix
  回填，回填后 NOT NULL）；
- catalog_id 转 nullable（provider_catalog 退役为推荐位，不再约束用户 Provider）；
- 活跃条目部分唯一索引重建：旧 (user_id, catalog_id, model_id)（0004）在
  catalog_id NULL 化后失去去重能力（PG 唯一索引 NULL 互异），改为
  (user_id, base_url, model_id) WHERE status='active'。

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_providers", sa.Column("base_url", sa.String(512)))
    op.execute(
        "UPDATE user_providers up SET base_url = 'https://' || pc.allowed_host || pc.path_prefix "
        "FROM provider_catalog pc WHERE pc.id = up.catalog_id AND up.base_url IS NULL"
    )
    op.alter_column("user_providers", "base_url", nullable=False)
    op.alter_column("user_providers", "catalog_id", nullable=True)
    op.alter_column("tasks", "provider_catalog_id", nullable=True)
    op.drop_index("uq_user_providers_active_entry", table_name="user_providers")
    op.create_index(
        "uq_user_providers_active_entry_v2",
        "user_providers",
        ["user_id", "base_url", "model_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_user_providers_active_entry_v2", table_name="user_providers")
    op.create_index(
        "uq_user_providers_active_entry",
        "user_providers",
        ["user_id", "catalog_id", "model_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.alter_column("tasks", "provider_catalog_id", nullable=False)
    op.alter_column("user_providers", "catalog_id", nullable=False)
    op.drop_column("user_providers", "base_url")
