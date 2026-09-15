"""owner 护栏生命周期放行修订（Phase 8 D3a 裁决；契约审查 C1）。

作者面 offline/DELETE 端点（Sup §10.6）在 owner 上下文（app role + GUC）需要两
类放行，0006/0007 护栏现状均 P0001 拦死（C1 三者叠加实证）：

1. 实体 status published→draft 翻转（offline 原语）——0006 对实体 status 变更
   无条件 RAISE → 修订为仅放行该单向翻转；其余状态变更（draft→published 自我
   发布面、published→archived 等）照旧 RAISE，词表冻结不变。
2. 实体删除的级联内部写——published_revision_id FK ondelete=SET NULL 在
   revision 删除时对实体行内部 UPDATE（0006 RAISE）、revision_tools FK
   ondelete=CASCADE 在 revision 删除时级联 DELETE（0007 RAISE）→ 以
   ``pg_trigger_depth() > 1`` 区分「FK/级联引发的内部触发」（放行）与「owner
   直接改写」（depth=1，照旧拦：发布指针冻结、非 draft 父行工具集冻结）。
   FK 动作写入的 NEW 值由系统固定（SET NULL / 级联删行），depth 通道不构成
   owner 可控的任意列改写面。
3. audit_logs 写授权 app role——offline/DELETE 的「同事务审计」（Sup §10.6：物理
   毁灭必须有审计痕）在 app role 零授权（0001:1023「审计属管理面」）下不可达，
   且跨 role 无同事务形态。本迁移收窄 0001 语义为「审计的读与治理处置属管理
   面」：INSERT 全表 + SELECT 限 created_at 单列（列级授权——SQLAlchemy 2.0
   eager_defaults 对 server_default 列发 INSERT...RETURNING，PG 要求对被返回列
   的 SELECT 特权；整行审计读仍 app 不可见）。owner 自助动作的审计 INSERT 由
   服务层固定 action/detail 写入（reason/request_id 为服务端字面量，无用户可控
   文本）。downgrade REVOKE 还原。

revision 内容冻结（content_json/content_sha256/revision_no/owner_id）、
draft→pending_review 流转、生而为 draft（INSERT guard）全部不变；
guard 函数 RAISE 消息与 0006/0007 逐字节一致（test_v2_rls 消息断言不漂移）。

测试钉点：tests/test_v2_author_lifecycle.py（护栏回归 + 服务生命周期）；
test_v2_migrations.py head==0011。asyncpg 单语句限制：每条 DDL 独立 op.execute。
"""

revision = "0011"
down_revision = "0009"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402

# 0006:34-62 原文修订：实体分支放行 published→draft + depth>1 内部 UPDATE；
# revision 分支与 INSERT guard 不动。
_GUARD_FUNCTION_0011 = """
CREATE OR REPLACE FUNCTION app.guard_owner_content_writes() RETURNS trigger AS $fn$
BEGIN
    IF app.current_owner_id() IS NULL THEN
        RETURN NEW;  -- admin/superuser 上下文：治理处置路径，放行
    END IF;
    -- 0011：FK/级联引发的内部 UPDATE（published_revision_id SET NULL）非直接
    -- owner 改写——depth>1 放行（值由 FK 动作固定，无注入面）
    IF pg_trigger_depth() > 1 THEN
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME IN ('experts', 'skills') THEN
        IF NEW.published_revision_id IS DISTINCT FROM OLD.published_revision_id THEN
            RAISE EXCEPTION 'owner 上下文禁止修改发布指针或实体状态';
        END IF;
        IF NEW.status IS DISTINCT FROM OLD.status
           AND NOT (OLD.status = 'published' AND NEW.status = 'draft') THEN
            RAISE EXCEPTION 'owner 上下文禁止修改发布指针或实体状态';
        END IF;
    ELSE  -- expert_revisions / skill_revisions（0006 主语义逐字保留）
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

# 0007:23-38 原文修订：depth>1（实体删除引发的级联 DELETE）放行；depth=1
# 直接改写仍按父 revision 冻结语义拦。
_TOOLS_GUARD_FUNCTION_0011 = """
CREATE OR REPLACE FUNCTION app.guard_owner_revision_tools() RETURNS trigger AS $fn$
BEGIN
    IF app.current_owner_id() IS NULL THEN
        RETURN COALESCE(NEW, OLD);  -- admin/superuser 上下文：治理处置/种子路径，放行
    END IF;
    -- 0011：实体删除引发的 revision_tools 级联 DELETE（FK CASCADE）非直接
    -- owner 改写——depth>1 放行
    IF pg_trigger_depth() > 1 THEN
        RETURN COALESCE(NEW, OLD);
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

# downgrade 还原原文（0006/0007 逐字节）
_GUARD_FUNCTION_0006 = """
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

_TOOLS_GUARD_FUNCTION_0007 = """
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
    op.execute(_GUARD_FUNCTION_0011)
    op.execute(_TOOLS_GUARD_FUNCTION_0011)
    # 同事务审计授权（offline/DELETE 审计痕）：INSERT 全表；SELECT 仅 created_at
    # 列（RETURNING 取 server_default 必需）——整行审计读维持管理面
    op.execute("GRANT INSERT ON audit_logs TO agentcraft_app")
    op.execute("GRANT SELECT (created_at) ON audit_logs TO agentcraft_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT (created_at) ON audit_logs FROM agentcraft_app")
    op.execute("REVOKE INSERT ON audit_logs FROM agentcraft_app")
    op.execute(_GUARD_FUNCTION_0006)
    op.execute(_TOOLS_GUARD_FUNCTION_0007)
