"""seed catalogs and slots

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09

Task 6（V2 Phase 1）种子：provider_catalog×3（OpenAI 含 v0.12.4 model_capabilities
图像输入声明 / DeepSeek / faux 仅开发环境且禁用）、tool_catalog×5（@version 1，
permissions JSONB）、platform_slots×2（free）、platform_storage×1（0 / 60GiB）。
种子行用 gen_random_uuid()（PG 内建 v4）：UUIDv7 语义在应用侧 default 生成，
种子无外部暴露问题。downgrade 四条 DELETE 逐条 op.execute（asyncpg 单语句限制）。
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Seed provider/tool catalogs, platform slots and storage singleton."""
    op.execute("""
    INSERT INTO provider_catalog
      (id, display_name, allowed_host, path_prefix, models, model_capabilities,
       healthcheck_method, healthcheck_path, enabled)
    VALUES
      (gen_random_uuid(), 'OpenAI', 'api.openai.com', '/v1',
       '["gpt-4o-mini","gpt-4o"]'::jsonb,
       '{"gpt-4o-mini":{"input":["text","image"]},"gpt-4o":{"input":["text","image"]}}'::jsonb,
       'GET', '/v1/models', true),
      (gen_random_uuid(), 'DeepSeek', 'api.deepseek.com', '/v1',
       '["deepseek-chat","deepseek-reasoner"]'::jsonb,
       NULL, 'GET', '/v1/models', true),
      (gen_random_uuid(), 'faux (dev only)', 'faux.invalid', '/v1',
       '["faux-echo"]'::jsonb,
       NULL, 'GET', '/v1/models', false);
    """)
    # 注 1：op.execute(str) 会经 sqlalchemy.text() 解析 :name 绑定参数，JSON 中的
    # "network":false 会被误当 bindparam —— 按官方转义规则写作 \:，编译后 SQL 与
    # brief 一字不差（text() 的 \: 转义在编译期剥除反斜杠）。
    # 注 2：query_task_state 的 permissions 过长，借 PG「换行分隔的相邻字符串字面量
    # 拼接」拆行（PG 文档 4.1.2.1），拼接结果与 JSONB 值逐字节一致。
    op.execute(r"""
    INSERT INTO tool_catalog (id, tool_id, version, permissions, enabled) VALUES
      (gen_random_uuid(), 'check_code_style', '1',
       '{"paths":["/task-files","/outputs"],"network"\:false}'::jsonb, true),
      (gen_random_uuid(), 'read_task_file', '1',
       '{"paths":["/task-files"],"network"\:false}'::jsonb, true),
      (gen_random_uuid(), 'write_output_file', '1',
       '{"paths":["/outputs"],"network"\:false}'::jsonb, true),
      (gen_random_uuid(), 'list_task_files', '1',
       '{"paths":["/task-files","/outputs"],"network"\:false}'::jsonb, true),
      (gen_random_uuid(), 'query_task_state', '1',
       '{"fields":["status","round_summary"],"exclude":["lease_owner","lease_epoch"],'
       '"network"\:false}'::jsonb, true);
    """)
    op.execute("INSERT INTO platform_slots (slot_no, state) VALUES (1,'free'), (2,'free');")
    op.execute(
        "INSERT INTO platform_storage (singleton, retained_storage_bytes, "
        "max_retained_storage_bytes) VALUES (true, 0, 64424509440);"
    )


def downgrade() -> None:
    """Remove seed rows (依赖序：storage → slots → tools → providers)."""
    op.execute("DELETE FROM platform_storage;")
    op.execute("DELETE FROM platform_slots;")
    op.execute("DELETE FROM tool_catalog;")
    op.execute("DELETE FROM provider_catalog;")
