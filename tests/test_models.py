from backend.models import Base

EXPECTED_TABLES = {
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
    "user_providers",
}


def test_orm_registers_expected_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
