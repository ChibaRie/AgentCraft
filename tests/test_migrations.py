import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_alembic_upgrade_head_creates_all_tables(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database_path.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = {row[0] for row in rows}
    expected = {
        "alembic_version",
        "users",
        "experts",
        "skills",
        "expert_skills",
        "tasks",
        "conversations",
        "messages",
        "task_files",
        "mcp_servers",
        "mcp_tools",
        "expert_mcps",
    }
    assert expected.issubset(tables)

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    indexes = {row[0] for row in rows}
    expected_indexes = {
        "idx_expert_mcps_server_id",
        "idx_expert_skills_skill_id",
        "idx_experts_owner_id",
        "idx_experts_status_category",
        "idx_mcp_servers_owner_id",
        "idx_mcp_tools_server_id",
        "idx_messages_conv_created",
        "idx_skills_owner_id",
        "idx_task_files_task_created",
        "idx_tasks_expert_id",
        "idx_tasks_user_id",
    }
    assert expected_indexes.issubset(indexes)
