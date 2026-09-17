"""V1 → V2 技能迁移（一次性工具，2026-09-17 用户裁决）。

把 V1 SQLite（agentcraft.db）skills 表的既有技能迁移为 V2 PG 技能实体：
- V1 skills 列与 V2 content_json 结构化字段**同构**（name/description/use_case/
  role/goal/steps/input_requirements/output_requirements/constraints），逐字段
  映射 + 边界钳位（V2 §9.8.2 词表：name 2-30 / description 10-200 / use_case
  20-5000 / role 5-200 / goal·steps·output_requirements·constraints 20-5000 /
  input_requirements ≤5000 可缺省）；
- V1 published → V2 published（绕审核直插，与 seed_v2_demo 同款：approved 终态
  + published_revision_id 回填）；V1 draft → V2 draft（无指针，保留可编辑语义）；
- 幂等：V2 中同名 content name 已存在即跳过；所有者=--owner-email（缺省 demo）；
- 防呆：published 直插仅允许 localhost DSN + 显式 --i-know-v1-migration。

用法（agentcraft/ 根目录）：
    export V2_DATABASE_URL=postgresql+asyncpg://<superuser>:<pw>@localhost:55432/agentcraft
    .venv/Scripts/python.exe tools/migrate_v1_skills.py --i-know-v1-migration
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import uuid as _uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_BANNER = """\
=== V1 → V2 技能迁移 —— 仅限本地开发 ===
把 V1 SQLite 技能迁移为 V2 技能实体（published 直插绕审核）；禁止指向任何
共享 / 远程 / 生产数据库（published 直插对非 localhost DSN 拒绝执行）。"""

_DECLARATION = """\
拒绝执行：本脚本会绕过内容审核直插 published 技能，仅限本地开发环境使用。
确认用途后请显式传入 --i-know-v1-migration 重跑。"""

_MIGRATION_NOTE = "（自 V1 迁移）"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate V1 SQLite skills into V2 PG (LOCAL DEVELOPMENT ONLY)."
    )
    parser.add_argument(
        "--i-know-v1-migration",
        action="store_true",
        help="确认知悉：published 直插绕过审核，仅限本地开发（必传）",
    )
    parser.add_argument("--sqlite", default="agentcraft.db", help="V1 SQLite 路径")
    parser.add_argument(
        "--dsn",
        default=None,
        help="V2 superuser DSN（缺省读 V2_DATABASE_URL；asyncpg 形态）",
    )
    parser.add_argument(
        "--owner-email", default="demo@example.com", help="迁移后技能所有者（V2 在册用户）"
    )
    return parser.parse_args(argv)


def _dsn_host(dsn: str) -> str | None:
    try:
        return urlparse(dsn).hostname
    except ValueError:
        return None


def _is_local_host(host: str | None) -> bool:
    return bool(host) and host.lower() in {"localhost", "127.0.0.1", "::1"}


def _fit(value: str | None, min_len: int, max_len: int, label: str) -> str:
    """钳位到 V2 词表边界：短则补迁移注记，长则截断（迁移场景的确定性处理）。"""
    text = (value or "").strip()
    if len(text) < min_len:
        text = (text + "；" if text else "") + label + _MIGRATION_NOTE + "。"
    if len(text) > max_len:
        text = text[: max_len - 3].rstrip() + "…"
    return text


def _load_v1_skills(sqlite_path: str) -> list[dict]:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, description, use_case, role, goal, steps, "
            "input_requirements, output_requirements, constraints, status "
            "FROM skills ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _to_content(row: dict, index: int) -> dict:
    """V1 行 → V2 content_json（1:1 映射 + 钳位；零字段丢失语义，仅边界适配）。"""
    name = (row["name"] or "").strip() or f"V1 技能 {index}"
    content = {
        "name": _fit(name, 2, 30, "技能"),
        "description": _fit(row["description"], 10, 200, "技能说明"),
        "use_case": _fit(row["use_case"], 20, 5000, "使用场景"),
        "role": _fit(row["role"], 5, 200, "担任角色"),
        "goal": _fit(row["goal"], 20, 5000, "目标"),
        "steps": _fit(row["steps"], 20, 5000, "执行步骤"),
        "output_requirements": _fit(row["output_requirements"], 20, 5000, "输出要求"),
        "constraints": _fit(row["constraints"], 20, 5000, "约束"),
    }
    input_requirements = (row["input_requirements"] or "").strip()
    if input_requirements:
        content["input_requirements"] = _fit(input_requirements, 1, 5000, "输入要求")
    return content


async def _migrate(args: argparse.Namespace, dsn: str) -> int:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from backend.v2.content_hash import content_sha256
    from backend.v2.ids import uuid7
    from backend.v2.models import Skill, SkillRevision

    v1_rows = _load_v1_skills(args.sqlite)
    engine = create_async_engine(dsn)
    outcomes: list[tuple[str, str]] = []
    try:
        async with engine.begin() as conn:
            owner_row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE email = :e"),
                    {"e": args.owner_email.lower()},
                )
            ).first()
            if owner_row is None:
                print(f"错误：所有者 {args.owner_email} 在 V2 库不存在（先建号再迁移）。")
                return 2
            owner_id = _uuid.UUID(str(owner_row[0]))

        async with AsyncSession(engine) as session:
            for index, row in enumerate(v1_rows, start=1):
                content = _to_content(row, index)
                existing = (
                    await session.execute(
                        text(
                            "SELECT s.id FROM skills s "
                            "JOIN skill_revisions r ON r.skill_id = s.id "
                            "WHERE r.content_json->>'name' = :n LIMIT 1"
                        ),
                        {"n": content["name"]},
                    )
                ).first()
                if existing is not None:
                    outcomes.append((content["name"], "exists"))
                    continue

                skill_id, revision_id = uuid7(), uuid7()
                is_published = (row["status"] or "").strip() == "published"
                session.add(
                    Skill(
                        id=skill_id,
                        owner_id=owner_id,
                        status="published" if is_published else "draft",
                    )
                )
                session.add(
                    SkillRevision(
                        id=revision_id,
                        skill_id=skill_id,
                        owner_id=owner_id,
                        revision_no=1,
                        content_json=content,
                        content_sha256=content_sha256(content),
                        status="published" if is_published else "draft",
                    )
                )
                await session.flush()
                if is_published:
                    await session.execute(
                        text("UPDATE skills SET published_revision_id = :r WHERE id = :s"),
                        {"r": revision_id, "s": skill_id},
                    )
                outcomes.append((content["name"], "published" if is_published else "draft"))
            await session.commit()
    finally:
        await engine.dispose()

    print("")
    print(f"迁移完成：{len(outcomes)} 项（所有者 {args.owner_email}）")
    for name, outcome in outcomes:
        print(f"  - [{outcome}] {name}")
    return 0


def main() -> int:
    args = _parse_args()
    print(_BANNER)
    if not args.i_know_v1_migration:
        print(_DECLARATION)
        return 2
    dsn = args.dsn or os.environ.get("V2_DATABASE_URL") or ""
    if not dsn:
        print("错误：未提供 DSN——传 --dsn 或设置 V2_DATABASE_URL（superuser，asyncpg 形态）。")
        return 2
    if not _is_local_host(_dsn_host(dsn)):
        print("拒绝：published 直插仅允许 localhost DSN。")
        return 2
    if not Path(args.sqlite).exists():
        print(f"错误：V1 SQLite 不存在：{args.sqlite}")
        return 2
    return asyncio.run(_migrate(args, dsn))


if __name__ == "__main__":
    raise SystemExit(main())
