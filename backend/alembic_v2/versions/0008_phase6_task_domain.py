"""Phase 6 任务域基建（D9 四项）：部分索引 + 终态意图位 + 产物轮次列。

(1) task_rounds 部分索引 ix_task_rounds_pending (task_id) WHERE state='pending'
    —— dispatcher 领取待执行轮的扫描面；
(2) tasks 部分索引 ix_tasks_terminal (status, created_at) WHERE status IN
    ('completed','failed','aborted','deleted') —— 终态保留期清扫（7 天）扫描面；
(3) task_files 加列 produced_in_round_id UUID NULL（产物来源轮），FK task_rounds
    ON DELETE SET NULL——轮行删除仅解引用不连带产物。FK 显式 create/drop
    （0001 :929 教训：use_alter 内联约束在单表 create_table 中不执行）；
    随附支撑索引 ix_task_files_produced_in_round_id（PG 不自动索引 FK 列，
    SET NULL 反查与产物列表扫描共用）；
(4) tasks 加列 pending_terminal VARCHAR(20) NULL（D19 终态意图位：running/queued
    期间的 complete/abort/delete 请求落列，轮收口事务读列定终态并清列），
    CHECK IN ('completed','aborted','deleted')。命名实况注记（Phase 10 收口对齐）：
    op.create_check_constraint 的显式名仍走 Base 的 ck 命名约定，二次前缀后 DB 上
    实际名为 ck_tasks_ck_tasks_pending_terminal_enum——模型 CheckConstraint 已按
    实况写同名，autogenerate 零差异。

模型层同步映射：backend/v2/models/tasking.py（Task.pending_terminal +
TaskFile.produced_in_round_id）。
测试钉点：tests/test_v2_migrations.py head==0008；tests/test_v2_migrations_0008.py
（存在性/SET NULL 行为/CHECK 拒绝）。downgrade 全量逆序。
asyncpg 单语句限制：每条 DDL 独立 op。
"""

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # noqa: E402
from alembic import op  # noqa: E402

_TERMINAL_WHERE = "status IN ('completed','failed','aborted','deleted')"


def upgrade() -> None:
    op.create_index(
        "ix_task_rounds_pending",
        "task_rounds",
        ["task_id"],
        unique=False,
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_tasks_terminal",
        "tasks",
        ["status", "created_at"],
        unique=False,
        postgresql_where=sa.text(_TERMINAL_WHERE),
    )
    op.add_column("task_files", sa.Column("produced_in_round_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_task_files_produced_in_round_id",
        "task_files",
        ["produced_in_round_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_task_files_produced_in_round_id_task_rounds",
        "task_files",
        "task_rounds",
        ["produced_in_round_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("tasks", sa.Column("pending_terminal", sa.String(length=20), nullable=True))
    op.create_check_constraint(
        "ck_tasks_pending_terminal_enum",
        "tasks",
        "pending_terminal IN ('completed', 'aborted', 'deleted')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tasks_pending_terminal_enum", "tasks", type_="check")
    op.drop_column("tasks", "pending_terminal")
    op.drop_constraint(
        "fk_task_files_produced_in_round_id_task_rounds", "task_files", type_="foreignkey"
    )
    op.drop_index("ix_task_files_produced_in_round_id", table_name="task_files")
    op.drop_column("task_files", "produced_in_round_id")
    op.drop_index(
        "ix_tasks_terminal", table_name="tasks", postgresql_where=sa.text(_TERMINAL_WHERE)
    )
    op.drop_index(
        "ix_task_rounds_pending",
        table_name="task_rounds",
        postgresql_where=sa.text("state = 'pending'"),
    )
