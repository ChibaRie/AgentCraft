"""seed_v2_admin 幂等断言（Phase 8 T7，Progress §5.6 转办「幂等断言加强」）。

pytest 调用脚本函数形态（tools/seed_v2_admin.py 独立脚本逻辑零改动）：对 pg
夹具克隆库（``pg.url()`` 即 superuser DSN——满足脚本「必须 superuser」红线，
users 表 ENABLE+FORCE RLS 且 admin role 无 INSERT policy）首跑返回 "seeded"、
二跑返回 "exists" 且**行零变化**（全列比对：二次运行不重置口令、不覆盖
role/status、不写 mfa_secret_enc）。
"""

import pytest
from sqlalchemy import text

from tools.seed_v2_admin import seed_admin


async def _row(pg, email: str) -> dict:
    async with pg.engine.connect() as conn:
        return dict(
            (
                await conn.execute(
                    text(
                        "SELECT id, email, password_hash, role, status, mfa_secret_enc, "
                        "created_at FROM users WHERE email = :e"
                    ),
                    {"e": email},
                )
            )
            .mappings()
            .one()
        )


@pytest.mark.asyncio
async def test_seed_admin_second_run_is_zero_change(pg):
    """首跑 seeded → 二跑 exists + 全列零变化（幂等不覆盖既有行）。"""
    dsn = pg.url()
    email = "Seed-Idem@Example.com"  # 大写入参：钉幂等键 = email 小写归一
    assert await seed_admin(dsn, email, "first-pw-1") == "seeded"
    before = await _row(pg, email.lower())
    assert before["role"] == "admin" and before["status"] == "active"
    assert before["mfa_secret_enc"] is None  # D9：未注册 TOTP 前永不过 admin 门

    assert await seed_admin(dsn, email, "second-pw-2") == "exists"
    after = await _row(pg, email.lower())
    assert after == before  # 二次运行零变化（password_hash 不被二次口令重置）
