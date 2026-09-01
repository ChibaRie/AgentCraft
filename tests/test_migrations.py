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
