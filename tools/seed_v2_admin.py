"""admin bootstrap（Phase 7 裁决 D9）：V2 PG superuser 播种 role='admin' 行（幂等）。

用法（agentcraft/ 根目录）：

    export V2_DATABASE_URL=postgresql+asyncpg://<superuser>:<pw>@<host>:<port>/<db>
    uv run python tools/seed_v2_admin.py admin@example.com --password '<初始密码>'

- DSN 缺省读环境变量 ``V2_DATABASE_URL``，``--dsn`` 显式覆盖。**必须为 superuser**：
  users 表 ENABLE+FORCE RLS，app role 仅 owner 自插、admin role 无 INSERT policy
  （0003/0009），只有 superuser 能播种。
- 幂等：email 已存在即跳过（exists），不改动既有行。
- mfa_secret_enc 置 NULL：首次使用经 /api/auth/mfa/setup + activate 注册 TOTP
  后方可过 admin 三重门（Phase 7 D2②——未配置者永不过门）。
- 不跑迁移：假设已 ``uv run alembic -c alembic_v2.ini upgrade head``。
- 独立脚本（D9）：V1 演示种子 seed_demo 已随 Phase 8 T13 cutover 删除，
  演示链在 T16 交付 seed_v2_demo 前依赖本脚本 + admin API 邀请流。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed a V2 admin user (idempotent; requires a superuser DSN)."
    )
    parser.add_argument("email", help="管理员登录邮箱（唯一键；已存在则跳过）")
    parser.add_argument(
        "--password", required=True, help="初始密码（服务端仅存 Argon2 哈希，明文不落库）"
    )
    parser.add_argument("--dsn", default=None, help="superuser DSN（缺省读 V2_DATABASE_URL）")
    return parser.parse_args(argv)


def _resolve_dsn(args: argparse.Namespace) -> str:
    dsn = args.dsn or os.environ.get("V2_DATABASE_URL") or ""
    if not dsn:
        print(
            "错误：未提供 DSN——传 --dsn 或设置 V2_DATABASE_URL 环境变量"
            "（须为 superuser 且 asyncpg 形态 postgresql+asyncpg://...）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return dsn


async def seed_admin(dsn: str, email: str, password: str) -> str:
    """播种/跳过 admin 行；返回 "seeded" | "exists"（幂等键 = email 小写归一）。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.v2.security import hash_password

    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    text("SELECT true FROM users WHERE email = :e"), {"e": email.lower()}
                )
            ).first()
            if existing is not None:
                return "exists"
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status, "
                    "mfa_secret_enc) VALUES (gen_random_uuid(), :e, :p, 'admin', "
                    "'active', NULL)"
                ),
                {"e": email.lower(), "p": hash_password(password)},
            )
    finally:
        await engine.dispose()
    return "seeded"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    outcome = asyncio.run(seed_admin(_resolve_dsn(args), args.email, args.password))
    if outcome == "exists":
        print(f"跳过：{args.email} 已存在（幂等，未改动既有行）")
    else:
        print(f"已播种 admin：{args.email}（role=admin, status=active, mfa_secret_enc=NULL）")
        print("首次使用请登录后完成 TOTP 注册（mfa/setup + activate），否则过不了 admin 门。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
