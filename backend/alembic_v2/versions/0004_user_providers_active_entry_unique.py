"""Phase 3（BYOK 目录化）：user_providers 活跃条目唯一索引。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-10

背景（裁决 D4）：Supplement §3 POST /api/providers 为纯创建语义，而 0001 仅建
uq_user_providers_one_default（默认互斥）——同 (user_id, catalog_id, model_id)
可插任意多行 active 且 V2 表无显示列可区分。加部分唯一索引：revoked 行不参与
约束，软撤（status→revoked）后同条目重添加天然可行。

Downgrade：DROP INDEX uq_user_providers_active_entry（逆向唯一动作）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_user_providers_active_entry",
        "user_providers",
        ["user_id", "catalog_id", "model_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_user_providers_active_entry", table_name="user_providers")
