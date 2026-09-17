"""RLS 测试共享助手（Phase 9 T6 拆分自 test_v2_rls.py）。

_role_engine 收编为 conftest.make_role_engine 的直接别名（本模块不再自带包装）；
跨段种子（_seed_user_with_task / _seed_bare_user / _published_ids_for）在此共享，
被 test_v2_rls（核心矩阵）、test_v2_rls_guards（0006/0007 护栏）、
test_v2_rls_migrations（Phase 6/7 增量面）共同消费。
"""

import uuid as _uuid

import pytest
from sqlalchemy import text

from tests.conftest import PgDb, make_role_engine

pytestmark = [pytest.mark.usefixtures("pg")]

OWNER_TABLES = (
    "tasks",
    "task_files",
    "task_messages",
    "task_rounds",
    "task_events",
    "user_providers",
    "sessions",
    "account_action_tokens",
    "email_outbox",
    "experts",
    "skills",
)


_role_engine = make_role_engine  # 收编：不得新增本地包装（Phase 6 T2 裁决）


async def _seed_user_with_task(pg: PgDb, email: str) -> _uuid.UUID:
    """superuser 造最小合法链：user → provider → expert → revision → task。

    superuser 只绕过 RLS、不绕过 FK 与 NOT NULL，因此必须用真实外键链
    （目录行来自 0002 种子），并显式给无 server default 的 NOT NULL 列
    （user_providers.is_default / tasks.event_sequence）赋值。
    """
    async with pg.engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), :e, 'h', 'user', 'active') RETURNING id"
                ),
                {"e": email},
            )
        ).scalar_one()
        catalog_id = (
            await conn.execute(text("SELECT id FROM provider_catalog LIMIT 1"))
        ).scalar_one()
        provider_id = (
            await conn.execute(
                text(
                    "INSERT INTO user_providers (id, user_id, catalog_id, base_url, model_id, "
                    "key_ciphertext, dek_wrapped, key_last4, key_version, status, is_default) "
                    "VALUES (gen_random_uuid(), :u, :c, 'https://rls-seed.example.com/v1', "
                    "'m', 'ct', 'dw', '4KEY', 1, 'active', false) RETURNING id"
                ),
                {"u": user_id, "c": catalog_id},
            )
        ).scalar_one()
        expert_id = (
            await conn.execute(
                text(
                    "INSERT INTO experts (id, owner_id, status) "
                    "VALUES (gen_random_uuid(), :u, 'published') RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
        revision_id = (
            await conn.execute(
                text(
                    "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) VALUES (gen_random_uuid(), :x, "
                    ":u, 1, '{}', :h, 'published') RETURNING id"
                ),
                {"x": expert_id, "u": user_id, "h": "a" * 64},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE experts SET published_revision_id = :r WHERE id = :x"),
            {"r": revision_id, "x": expert_id},
        )
        await conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, expert_revision_id, provider_id, "
                "provider_catalog_id, provider_model_id, provider_key_version, "
                "event_sequence, status) VALUES (gen_random_uuid(), :u, :r, :p, :c, "
                "'m', 1, 0, 'uploading')"
            ),
            {"u": user_id, "r": revision_id, "p": provider_id, "c": catalog_id},
        )
    return user_id


async def _seed_bare_user(pg: PgDb, email: str) -> _uuid.UUID:
    """superuser 造一个无任何数据行的用户（充当陌生 owner 的真实 user id）。"""
    async with pg.engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), :e, 'h', 'user', 'active') RETURNING id"
                ),
                {"e": email},
            )
        ).scalar_one()


async def _published_ids_for(
    pg: PgDb, table: str, owner: _uuid.UUID
) -> tuple[_uuid.UUID, _uuid.UUID]:
    """superuser 查指定 owner 在 experts/skills 表的 (行 id, published_revision_id)。"""
    assert table in ("experts", "skills")
    async with pg.engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    f"SELECT id, published_revision_id FROM {table} WHERE owner_id = :u ORDER BY id"
                ),
                {"u": owner},
            )
        ).one()
    return row[0], row[1]
