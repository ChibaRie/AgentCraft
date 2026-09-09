import pytest
from sqlalchemy import text

pytestmark = [pytest.mark.usefixtures("pg")]


async def test_pg_fixture_boots_and_runs_sql(pg):
    async with pg.engine.begin() as conn:
        val = (await conn.execute(text("SELECT version()"))).scalar_one()
        assert "PostgreSQL 16" in val or "PostgreSQL 17" in val
