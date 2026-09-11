"""Phase 3 收口热修：users 删除期限 CHECK 约束重命名（对齐模型渲染名）。

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-11

背景：0003 的 op.create_check_constraint 传入了已含 `ck_users_` 前缀的名字，
而 alembic 命名约定 ck_%(table_name)s_%(constraint_name)s 对其二次前缀，
DB 实际名为 ck_users_ck_users_deletion_deadline_consistency；模型
identity.py 声明 name="deletion_deadline_consistency"，渲染为单前缀
ck_users_deletion_deadline_consistency → alembic check 报
remove/add constraint 漂移（同名不同名实为约束名错位）。

迁移 append-only（不修改 0003）：本迁移以 RENAME CONSTRAINT 把 DB 名
对齐模型渲染名。确定性论证：命名约定渲染是确定性的，凡执行过 0003
的库必有双前缀旧名，故 RENAME 可无条件执行。

Downgrade：逆向 RENAME 回双前缀旧名。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_NAME = "ck_users_ck_users_deletion_deadline_consistency"
_NEW_NAME = "ck_users_deletion_deadline_consistency"


def upgrade() -> None:
    op.execute(f"ALTER TABLE users RENAME CONSTRAINT {_OLD_NAME} TO {_NEW_NAME}")


def downgrade() -> None:
    op.execute(f"ALTER TABLE users RENAME CONSTRAINT {_NEW_NAME} TO {_OLD_NAME}")
