"""迁移 0008（Phase 6 T2，D9 四项）验收：三索引/两列存在性 + SET NULL 行为 + CHECK。

模板库（conftest.pg_template）已 upgrade head 至 0008——pg 夹具克隆即含新对象；
upgrade/downgrade 往返由 tests/test_v2_migrations.py 全链循环覆盖。
owner-RLS 表种子一律 superuser 连接（tests/v2_provider_helpers 惯例）。
"""

import uuid as _uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = [pytest.mark.usefixtures("pg")]

NEW_INDEXES = (
    "ix_task_rounds_pending",
    "ix_tasks_terminal",
    "ix_task_files_produced_in_round_id",
)


async def _seed_task_chain(pg) -> dict[str, _uuid.UUID]:
    """superuser 造最小合法链：user → provider → expert → revision → task → message → round。

    返回关键 id（蓝本 tests/v2_provider_helpers.seed_task_for_provider 的 queued 形态：
    task_rounds 需先有 source_message 消息行；lease_epoch/attempt NOT NULL 显式赋 0）。
    """
    async with pg.engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), 't0008@x.test', 'h', 'user', 'active') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        catalog_id = (
            await conn.execute(text("SELECT id FROM provider_catalog LIMIT 1"))
        ).scalar_one()
        provider_id = (
            await conn.execute(
                text(
                    "INSERT INTO user_providers (id, user_id, catalog_id, model_id, "
                    "key_ciphertext, dek_wrapped, key_last4, key_version, status, is_default) "
                    "VALUES (gen_random_uuid(), :u, :c, 'm', 'ct', 'dw', '4KEY', 1, "
                    "'active', false) RETURNING id"
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
        task_id = (
            await conn.execute(
                text(
                    "INSERT INTO tasks (id, owner_id, expert_revision_id, provider_id, "
                    "provider_catalog_id, provider_model_id, provider_key_version, "
                    "event_sequence, status) VALUES (gen_random_uuid(), :u, :r, :p, :c, "
                    "'m', 1, 0, 'running') RETURNING id"
                ),
                {"u": user_id, "r": revision_id, "p": provider_id, "c": catalog_id},
            )
        ).scalar_one()
        message_id = (
            await conn.execute(
                text(
                    "INSERT INTO task_messages (id, task_id, owner_id, event_sequence, "
                    "author, content) VALUES (gen_random_uuid(), :t, :u, 1, 'user', 'seed') "
                    "RETURNING id"
                ),
                {"t": task_id, "u": user_id},
            )
        ).scalar_one()
        round_id = (
            await conn.execute(
                text(
                    "INSERT INTO task_rounds (id, task_id, owner_id, source_message_id, "
                    "state, lease_epoch, attempt) "
                    "VALUES (gen_random_uuid(), :t, :u, :m, 'pending', 0, 0) RETURNING id"
                ),
                {"t": task_id, "u": user_id, "m": message_id},
            )
        ).scalar_one()
    return {
        "user_id": user_id,
        "task_id": task_id,
        "message_id": message_id,
        "round_id": round_id,
    }


async def test_0008_indexes_and_columns_present(pg) -> None:
    """三索引（两条件部分索引 + FK 支撑索引）与两列存在；谓词/长度钉字面。"""
    names = ",".join(f"'{n}'" for n in NEW_INDEXES)  # 表/索引名来自模块常量，无注入面
    async with pg.engine.begin() as conn:
        defs = {
            row[0]: row[1]
            for row in (
                await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        f"WHERE schemaname = 'public' AND indexname IN ({names})"
                    )
                )
            ).all()
        }
        cols = {
            row[0]: (row[1], row[2])
            for row in (
                await conn.execute(
                    text(
                        "SELECT table_name || '.' || column_name, data_type, "
                        "character_maximum_length FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND "
                        "((table_name = 'tasks' AND column_name = 'pending_terminal') OR "
                        "(table_name = 'task_files' AND "
                        "column_name = 'produced_in_round_id'))"
                    )
                )
            ).all()
        }
    assert set(defs) == set(NEW_INDEXES)
    # 谓词反解形态随 PG 版本可能加括号/::text 转型，放宽到字面包含（0004 先例）
    assert "'pending'" in defs["ix_task_rounds_pending"]
    for status in ("completed", "failed", "aborted", "deleted"):
        assert f"'{status}'" in defs["ix_tasks_terminal"]
    assert cols == {
        "tasks.pending_terminal": ("character varying", 20),
        "task_files.produced_in_round_id": ("uuid", None),
    }


async def test_0008_explicit_fk_exists(pg) -> None:
    """FK 显式补建且名与模型命名约定产物一字不差（0001 :929 教训）。"""
    async with pg.engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conname = 'fk_task_files_produced_in_round_id_task_rounds' "
                    "AND contype = 'f'"
                )
            )
        ).one_or_none()
    assert row is not None
    # confdeltype 'n' = SET NULL；asyncpg 对 "char" 列回传 bytes
    assert row[0] in ("n", b"n")


async def test_produced_in_round_id_set_null_on_round_delete(pg) -> None:
    """产物来源轮 FK ON DELETE SET NULL：删轮行仅解引用，产物行保留。"""
    ids = await _seed_task_chain(pg)
    file_id = _uuid.uuid4()
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO task_files (id, task_id, owner_id, direction, file_name, "
                "storage_key, sha256, size_bytes, state, produced_in_round_id) VALUES "
                "(:f, :t, :u, 'output', 'a.txt', 'tasks/x/y', :h, 1, 'registered', :r)"
            ),
            {
                "f": file_id,
                "t": ids["task_id"],
                "u": ids["user_id"],
                "h": "b" * 64,
                "r": ids["round_id"],
            },
        )
        await conn.execute(text("DELETE FROM task_rounds WHERE id = :r"), {"r": ids["round_id"]})
        produced = (
            await conn.execute(
                text("SELECT produced_in_round_id FROM task_files WHERE id = :f"),
                {"f": file_id},
            )
        ).scalar_one()
        kept = (
            await conn.execute(
                text("SELECT count(*) FROM task_files WHERE id = :f"), {"f": file_id}
            )
        ).scalar_one()
    assert produced is None
    assert kept == 1


async def test_pending_terminal_check_rejects_outside_lexicon(pg) -> None:
    """CHECK（ck_tasks_pending_terminal_enum）拒绝词表外值；词表内三值与 NULL 放行。"""
    ids = await _seed_task_chain(pg)
    async with pg.engine.begin() as conn:
        for value in ("completed", "aborted", "deleted", None):
            await conn.execute(
                text("UPDATE tasks SET pending_terminal = :v WHERE id = :t"),
                {"v": value, "t": ids["task_id"]},
            )
    with pytest.raises(IntegrityError) as excinfo:
        async with pg.engine.begin() as conn:
            await conn.execute(
                text("UPDATE tasks SET pending_terminal = 'bogus' WHERE id = :t"),
                {"t": ids["task_id"]},
            )
    assert excinfo.value.orig.sqlstate == "23514"  # check_violation
    async with pg.engine.begin() as conn:  # 失败事务已回滚，行原样且列仍 NULL
        value = (
            await conn.execute(
                text("SELECT pending_terminal FROM tasks WHERE id = :t"), {"t": ids["task_id"]}
            )
        ).scalar_one()
    assert value is None
