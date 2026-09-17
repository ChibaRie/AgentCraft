"""任务快照挂载列（Phase 10 M4）：tasks.mcp_servers JSONB。

- 创建事务内冻结 mcp_refs → [{server_id, name, transport_kind}] 三键描述符
  （user_mcp_servers 命令材料信封不变，快照只冻结描述符；明文/密文均不入
  任务快照）；任务期不可变——PUT（改名/停用）/删除 server 不回写任务，执行期
  缺失/停用静默剔除（kill switch 任务侧联动属 M7）；
- 无 FK 无 RLS 变更：列随 tasks 既有 owner-RLS 行存取；单语句 DDL（asyncpg
  限制惯例）。

测试钉点：tests/test_v2_mcp_tasks.py（快照冻结/校验矩阵/视图回显/装配函数）；
test_v2_migrations.py head==0014。

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("mcp_servers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "mcp_servers")
