"""V2 演示种子（Phase 8 T16）：本地开发一键演示链（幂等；安全审查 I-3 防呆三件套）。

用法（agentcraft/ 根目录；**仅限本地开发**）：

    export V2_DATABASE_URL=postgresql+asyncpg://<superuser>:<pw>@localhost:5432/<db>
    uv run python tools/seed_v2_demo.py --i-know-demo-bypasses-review

演示链五段（全部幂等；email / 内容名键）：

1. admin 种子：复用 ``tools/seed_v2_admin.seed_admin``（role=admin，mfa 待注册）；
2. 邀请行：superuser 直插（token 仅哈希入库，**明文仅本次 stdout 打印一次**）；
   demo 用户激活走真实 API（``POST /api/auth/invitations/accept``），本脚本永不
   直插 demo 用户行——激活后重跑本脚本即补建后续演示数据；
3. faux Provider 目录启用：0002 种子行（'faux (dev only)' / faux.invalid）置
   enabled=true（**仅 dev 可达**：published 段对非 localhost DSN 拒绝执行）；
4. published expert/skill 直插：approved 终态（published revision）绕审核，含
   skill_refs 引用链；
5. 示例任务（demo 用户在册时）：复用 task_service.create_task + commit_input
   真实写链（uploading → commit → queued + pending 初始轮）；provider 为 demo
   用户名下 faux 密文行（dummy key 经 KEK 信封封装）。

防呆三件套（安全审查 I-3，硬验收）：

- ① ``--i-know-demo-bypasses-review`` 显式 flag 前置：未带即退出并打印用途声明；
- ② DSN host 非 localhost/127.0.0.1/::1 → 拒绝直插 published 段（邀请/用户段放行）；
- ③ 密码 ``--admin-password`` / ``--demo-password`` 传参，缺省随机生成一次性打印，
  零硬编码。

不跑迁移：假设已 ``uv run alembic -c alembic_v2.ini upgrade head``。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import uuid as _uuid
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.seed_v2_admin import seed_admin  # noqa: E402

_BANNER = """\
=== AgentCraft V2 演示种子 —— 仅限本地开发 ===
本脚本直插演示数据并绕过内容审核链（published 直插 + faux Provider 启用），
禁止指向任何共享 / 远程 / 生产数据库（published 段对非 localhost DSN 拒绝执行）。"""

_DECLARATION = """\
拒绝执行：本脚本会直插演示数据并绕过内容审核（published expert/skill 直插 +
faux Provider 目录启用），仅限本地开发环境使用。
确认用途后请显式传入 --i-know-demo-bypasses-review 重跑。"""

_PUBLISHED_REFUSAL = (
    "拒绝直插 published 段：DSN host（{host}）非 localhost/127.0.0.1/::1。\n"
    "邀请/用户段已放行完成；published expert/skill 与示例任务须指向本地库。"
)

_FAUX_HOST = "faux.invalid"
_FAUX_MODEL = "faux-echo"
_FAUX_DISPLAY_NAME = "faux (dev only)"
_FAUX_DUMMY_KEY = "sk-demo-faux-local-only"  # 演示占位 Key（非真实凭据）；last4 取原样末 4 位

_SKILL_CONTENT = {
    "name": "演示·结构化代码评审",
    "description": "演示种子技能：按四维清单对代码片段做结构化评审并输出改进建议。",
    "use_case": "演示环境中体验 Skill 引用链：提交一段代码，按固定清单给出评审意见与改进建议。",
    "role": "资深代码评审顾问",
    "goal": "对输入代码输出结构化评审：问题清单、风险分级与可执行的改进建议。",
    "steps": "1) 通读代码并总结意图；2) 按正确性/可读性/性能/安全四维逐项检查；"
    "3) 汇总为结构化评审清单输出。",
    "input_requirements": "一段源代码文本（任意语言），可附一句话说明意图。",
    "output_requirements": "Markdown 评审清单：每条含【维度】【严重度】【问题】【建议】四字段。",
    "constraints": "仅评审不重写；不执行代码；结论必须引用代码行内容。",
}

_EXPERT_NAME = "演示·代码评审顾问"

_INITIAL_MESSAGE = (
    "请评审下面这段代码并给出改进建议：\n\n```python\ndef add(a, b):\n    return a + b\n```\n"
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the V2 demo chain (LOCAL DEVELOPMENT ONLY; "
        "requires a superuser DSN and an explicit bypass flag)."
    )
    parser.add_argument(
        "--i-know-demo-bypasses-review",
        action="store_true",
        help="确认知悉：本脚本直插演示数据并绕过内容审核，仅限本地开发（必传）",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="superuser DSN（缺省读 V2_DATABASE_URL；asyncpg 形态 postgresql+asyncpg://...）",
    )
    parser.add_argument(
        "--admin-email",
        default="admin@example.com",
        help="admin 登录邮箱（唯一键；已存在则跳过，缺省 admin@example.com）",
    )
    parser.add_argument(
        "--admin-password",
        default=None,
        help="admin 初始密码（Argon2 落库；缺省随机生成一次性打印）",
    )
    parser.add_argument(
        "--demo-email",
        default="demo@example.com",
        help="demo 用户邮箱（邀请键；激活经真实 API，缺省 demo@example.com）",
    )
    parser.add_argument(
        "--demo-password",
        default=None,
        help="demo 用户建议密码（接受邀请时使用；缺省随机生成一次性打印）",
    )
    parser.add_argument(
        "--expires-days",
        type=int,
        default=7,
        help="邀请有效期天数（1..7，缺省 7）",
    )
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


def _dsn_host(dsn: str) -> str | None:
    """DSN → host（解析失败 / 无 host 形态返回 None，一律按非本地处置）。"""
    try:
        return urlparse(dsn).hostname
    except ValueError:
        return None


def _is_local_host(host: str | None) -> bool:
    """localhost/127.0.0.1/::1（大小写不敏感）才算本地；None 保守拒绝。"""
    if not host:
        return False
    return host.lower() in {"localhost", "127.0.0.1", "::1"}


def _engine(dsn: str):
    """演示链唯一引擎工厂（测试桩点：防呆守卫用例经此重定向真实库）。"""
    from sqlalchemy.ext.asyncio import create_async_engine

    return create_async_engine(dsn)


def _resolve_password(value: str | None, label: str) -> str:
    """密码解析（防呆③）：显式传参优先，缺省随机生成一次性打印，零硬编码。"""
    if value:
        return value
    generated = secrets.token_urlsafe(12)
    print(f"{label}（随机生成，仅本次打印）: {generated}")
    return generated


def _print_activation_hint(demo_email: str, demo_password: str, has_token: bool) -> None:
    """demo 用户激活路径说明（邀请接受走真实 API；token 明文以首次打印为准）。"""
    token_note = "上方邀请 token" if has_token else "首次播种打印的邀请 token（明文不可复得）"
    print("")
    print("demo 用户激活路径（走真实 API；本脚本不直插用户行）：")
    print(
        "  curl -X POST http://127.0.0.1:8000/api/auth/invitations/accept "
        '-H "Content-Type: application/json" '
        f'-d \'{{"invitation_token":"<{token_note}>","email":"{demo_email}",'
        f'"password":"{demo_password}"}}\''
    )
    print("  或在前端登录页使用邀请激活入口。激活后重跑本脚本补建示例任务。")


async def _ensure_invitation(
    engine, *, demo_email: str, admin_email: str, expires_days: int
) -> tuple[str, str | None]:
    """邀请段（幂等，email 键）：返回 (outcome, token 明文或 None)。

    - demo 用户已在册 → "user-exists"（激活已完成，不再签发）；
    - 未消费未撤销邀请在册 → "open-exists"（明文仅首次打印，不可复得）；
    - 其余 → superuser 直插新邀请行（admin 在册时挂 created_by）。
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from backend.v2.ids import uuid7
    from backend.v2.security import generate_token, hash_token

    async with engine.begin() as conn:
        user = (
            await conn.execute(
                text("SELECT id FROM users WHERE email = :e"), {"e": demo_email.lower()}
            )
        ).first()
        if user is not None:
            return "user-exists", None
        open_row = (
            await conn.execute(
                text(
                    "SELECT id FROM invitations WHERE email = :e "
                    "AND consumed_at IS NULL AND revoked_at IS NULL"
                ),
                {"e": demo_email.lower()},
            )
        ).first()
        if open_row is not None:
            return "open-exists", None
        admin = (
            await conn.execute(
                text("SELECT id FROM users WHERE email = :e"), {"e": admin_email.lower()}
            )
        ).first()
        token = generate_token()
        await conn.execute(
            text(
                "INSERT INTO invitations (id, token_hash, email, expires_at, created_by) "
                "VALUES (:id, :h, :e, :exp, :by)"
            ),
            {
                "id": uuid7(),
                "h": hash_token(token),
                "e": demo_email.lower(),
                "exp": datetime.now(timezone.utc) + timedelta(days=expires_days),
                "by": admin[0] if admin is not None else None,
            },
        )
    return "created", token


async def _enable_faux_catalog(engine) -> dict:
    """faux Provider 目录段：0002 种子行置 enabled（幂等）；缺行时按 0002 形态补插。"""
    from sqlalchemy import text

    from backend.v2.ids import uuid7

    async with engine.begin() as conn:
        flipped = await conn.execute(
            text(
                "UPDATE provider_catalog SET enabled = true "
                "WHERE allowed_host = :h AND enabled = false"
            ),
            {"h": _FAUX_HOST},
        )
        if flipped.rowcount:
            outcome = "enabled"
        else:
            existing = (
                await conn.execute(
                    text("SELECT id FROM provider_catalog WHERE allowed_host = :h"),
                    {"h": _FAUX_HOST},
                )
            ).first()
            outcome = "already-enabled" if existing is not None else "inserted"
            if existing is None:
                await conn.execute(
                    text(
                        "INSERT INTO provider_catalog (id, display_name, allowed_host, "
                        "path_prefix, models, model_capabilities, healthcheck_method, "
                        "healthcheck_path, enabled) "
                        "VALUES (:id, :dn, :h, '/v1', :models, NULL, 'GET', '/v1/models', true)"
                    ),
                    {
                        "id": uuid7(),
                        "dn": _FAUX_DISPLAY_NAME,
                        "h": _FAUX_HOST,
                        "models": f'["{_FAUX_MODEL}"]',
                    },
                )
        row = (
            await conn.execute(
                text("SELECT id, models FROM provider_catalog WHERE allowed_host = :h"),
                {"h": _FAUX_HOST},
            )
        ).one()
    return {"outcome": outcome, "catalog_id": row[0], "model_id": list(row[1])[0]}


async def _ensure_published_content(engine, *, owner_id: _uuid.UUID) -> dict:
    """published expert/skill 直插段（幂等，content name 键；绕审核 approved 终态）。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.v2.content_hash import content_sha256
    from backend.v2.ids import uuid7
    from backend.v2.models import Expert, ExpertRevision

    async with AsyncSession(engine) as session:
        async with session.begin():
            skill_row = (
                await session.execute(
                    text(
                        "SELECT s.id, r.id FROM skills s "
                        "JOIN skill_revisions r ON r.skill_id = s.id "
                        "WHERE r.content_json->>'name' = :n LIMIT 1"
                    ),
                    {"n": _SKILL_CONTENT["name"]},
                )
            ).first()
            skill_id, skill_rev_id = (
                (skill_row[0], skill_row[1])
                if skill_row is not None
                else await _insert_skill(session, owner_id)
            )

            expert_content = {
                "name": _EXPERT_NAME,
                "description": "演示种子专家：引用演示技能为代码片段提供结构化评审意见。",
                "category": "tech",
                "avatar_url": None,
                "persona": "严谨、直接的资深评审者，先肯定优点再指出问题，语气克制、结论先行。",
                "methodology": "先读意图，再按正确性/可读性/性能/安全四维扫描，"
                "最后给出分级改进清单。",
                "task_examples": ["评审这段 Python 函数", "检查这段 SQL 的注入风险"],
                "skill_refs": [{"skill_id": str(skill_id), "revision_id": str(skill_rev_id)}],
            }
            expert_row = (
                await session.execute(
                    text(
                        "SELECT e.id, r.id FROM experts e "
                        "JOIN expert_revisions r ON r.expert_id = e.id "
                        "WHERE r.content_json->>'name' = :n LIMIT 1"
                    ),
                    {"n": _EXPERT_NAME},
                )
            ).first()
            if expert_row is None:
                expert_id, expert_rev_id = uuid7(), uuid7()
                session.add(Expert(id=expert_id, owner_id=owner_id, status="published"))
                session.add(
                    ExpertRevision(
                        id=expert_rev_id,
                        expert_id=expert_id,
                        owner_id=owner_id,
                        revision_no=1,
                        content_json=expert_content,
                        content_sha256=content_sha256(expert_content),
                        status="published",
                    )
                )
                await session.flush()
                await session.execute(
                    text("UPDATE experts SET published_revision_id = :r WHERE id = :e"),
                    {"r": expert_rev_id, "e": expert_id},
                )
            else:
                expert_rev_id = expert_row[1]
    return {"expert_revision_id": str(expert_rev_id), "skill_revision_id": str(skill_rev_id)}


async def _insert_skill(session, owner_id: _uuid.UUID) -> tuple[_uuid.UUID, _uuid.UUID]:
    """演示 skill + published revision 直插（含 published 指针回填）；返回 (id, rev_id)。"""
    from sqlalchemy import text

    from backend.v2.content_hash import content_sha256
    from backend.v2.ids import uuid7
    from backend.v2.models import Skill, SkillRevision

    skill_id, skill_rev_id = uuid7(), uuid7()
    session.add(Skill(id=skill_id, owner_id=owner_id, status="published"))
    session.add(
        SkillRevision(
            id=skill_rev_id,
            skill_id=skill_id,
            owner_id=owner_id,
            revision_no=1,
            content_json=_SKILL_CONTENT,
            content_sha256=content_sha256(_SKILL_CONTENT),
            status="published",
        )
    )
    await session.flush()
    await session.execute(
        text("UPDATE skills SET published_revision_id = :r WHERE id = :s"),
        {"r": skill_rev_id, "s": skill_id},
    )
    return skill_id, skill_rev_id


async def _ensure_demo_provider(session, *, owner_id: _uuid.UUID, catalog: dict) -> str:
    """demo 用户名下 faux provider 行（幂等：已有 active provider 即复用）。

    dummy key 经 KEK 信封封装；KEK 缺失 → 干净报错（调用方转退出码 2，零半账）。
    """
    from sqlalchemy import select

    from backend.v2.ids import uuid7
    from backend.v2.models import UserProvider
    from backend.v2.provider_crypto import key_sealer

    existing = (
        await session.execute(
            select(UserProvider.id)
            .where(UserProvider.user_id == owner_id, UserProvider.status == "active")
            .order_by(UserProvider.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return str(existing)
    provider_id = uuid7()
    key_ciphertext, dek_wrapped = key_sealer().seal(_FAUX_DUMMY_KEY, provider_id=str(provider_id))
    session.add(
        UserProvider(
            id=provider_id,
            user_id=owner_id,
            catalog_id=_uuid.UUID(str(catalog["catalog_id"])),
            model_id=catalog["model_id"],
            key_ciphertext=key_ciphertext,
            dek_wrapped=dek_wrapped,
            key_last4=_FAUX_DUMMY_KEY[-4:],
            key_version=1,
            status="active",
            is_default=True,
        )
    )
    await session.flush()
    return str(provider_id)


async def _ensure_demo_task(engine, *, owner_id: _uuid.UUID, catalog: dict, content: dict) -> dict:
    """示例任务段（幂等：同 owner+revision 已有任务即跳过）。

    复用 task_service.create_task（uploading+初始消息+active 预留+配额推进）与
    commit_input（staged→committed+task_root 预留+初始 pending 轮+queued 翻转+
    事件对）真实写链；superuser 会话绕过 RLS（GUC 免设）；provider 与任务同事务
    （RESTRICT FK 可见性）。
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.v2.models import Task
    from backend.v2.task_service import commit_input, create_task

    async with AsyncSession(engine) as session:
        async with session.begin():
            existing = (
                await session.execute(
                    select(Task.id).where(
                        Task.owner_id == owner_id,
                        Task.expert_revision_id == _uuid.UUID(content["expert_revision_id"]),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {"outcome": "exists", "task_id": str(existing)}
            provider_id = await _ensure_demo_provider(session, owner_id=owner_id, catalog=catalog)
            created = await create_task(
                session,
                owner_id=str(owner_id),
                expert_revision_id=content["expert_revision_id"],
                provider_id=provider_id,
                initial_message=_INITIAL_MESSAGE,
                provider_snapshot={
                    "provider_catalog_id": str(catalog["catalog_id"]),
                    "provider_model_id": catalog["model_id"],
                    "provider_key_version": 1,
                },
            )
            task_id = created["task"]["id"]
            committed = await commit_input(
                session, owner_id=str(owner_id), task_id=task_id, manifest=[]
            )
    return {
        "outcome": "created",
        "task_id": created["task"]["id"],
        "round_id": committed["round_id"],
    }


async def _user_id(engine, email: str) -> _uuid.UUID | None:
    """email → users.id（无行返回 None；superuser 绕过 RLS）。"""
    from sqlalchemy import text

    async with engine.connect() as conn:
        row = (
            await conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email.lower()})
        ).first()
    return row[0] if row is not None else None


def _print_guidance() -> None:
    """演示链完整使用指引（登录/激活/建任务/加 admin 的完整路径）。"""
    print("")
    print("演示链使用指引：")
    print("  1. 启动后端 uvicorn backend.main:app（V2 双 DSN）与前端 npm run dev；")
    print(
        "  2. admin 登录 http://127.0.0.1:5173/login → 首次登录完成 TOTP 注册"
        "（mfa/setup + activate）后方可进 admin 面；"
    )
    print(
        "  3. demo 用户按上方路径激活邀请 → 激活后重跑本脚本补建示例任务"
        "（faux provider + uploading→queued 全链）；"
    )
    print("  4. demo 登录 → 专家中心选「演示·代码评审顾问」→ 建任务（faux-echo）→ 提交观察；")
    print(
        "  5. 再增管理员：uv run python tools/seed_v2_admin.py <email> --password '<初始密码>'，"
        "或 admin 控制台用户页调整角色。"
    )


async def _run(args: argparse.Namespace) -> int:
    """演示链编排：邀请/用户段恒执行；published 段仅本地 DSN 放行（防呆②）。"""
    if not 1 <= args.expires_days <= 7:
        print("错误：--expires-days 须为 1..7 的整数", file=sys.stderr)
        return 2
    dsn = _resolve_dsn(args)
    allow_published = _is_local_host(_dsn_host(dsn))
    admin_password = _resolve_password(args.admin_password, "admin 初始密码")
    demo_password = _resolve_password(args.demo_password, "demo 用户建议密码")

    engine = _engine(dsn)
    try:
        admin_outcome = await seed_admin(dsn, args.admin_email, admin_password)
        if admin_outcome == "exists":
            print(f"跳过：{args.admin_email} 已存在（幂等，未改动既有行）")
        else:
            print(
                f"已播种 admin：{args.admin_email}"
                "（role=admin, status=active, mfa_secret_enc=NULL）"
            )

        outcome, token = await _ensure_invitation(
            engine,
            demo_email=args.demo_email,
            admin_email=args.admin_email,
            expires_days=args.expires_days,
        )
        if outcome == "user-exists":
            print(f"跳过邀请：{args.demo_email} 已在册（demo 用户已激活）")
        elif outcome == "open-exists":
            print(
                f"跳过邀请：{args.demo_email} 已有未消费邀请"
                "（明文 token 仅首次播种时打印一次，不可复得）"
            )
            _print_activation_hint(args.demo_email, demo_password, has_token=False)
        else:
            print(
                f"已直插邀请行：{args.demo_email}"
                f"（有效期 {args.expires_days} 天，token 仅哈希入库）"
            )
            print(f"邀请 token：{token}")
            _print_activation_hint(args.demo_email, demo_password, has_token=True)

        if not allow_published:
            print(_PUBLISHED_REFUSAL.format(host=_dsn_host(dsn) or "<unparsable>"))
            return 2

        catalog = await _enable_faux_catalog(engine)
        print(f"faux 目录条目已启用（{catalog['outcome']}）：{_FAUX_HOST} / {catalog['model_id']}")

        admin_id = await _user_id(engine, args.admin_email)
        if admin_id is None:
            print("错误：admin 行缺失，无法直插 published 内容", file=sys.stderr)
            return 2
        content = await _ensure_published_content(engine, owner_id=admin_id)
        print(
            "published expert/skill 已直插（绕审核，approved 终态）："
            f"expert revision {content['expert_revision_id']}"
        )

        demo_id = await _user_id(engine, args.demo_email)
        if demo_id is None:
            print(f"示例任务待建：{args.demo_email} 尚未激活——接受邀请后重跑本脚本补建。")
        else:
            try:
                task = await _ensure_demo_task(
                    engine, owner_id=demo_id, catalog=catalog, content=content
                )
            except ValueError as exc:
                print(f"错误：示例任务段失败（{exc}）", file=sys.stderr)
                return 2
            if task["outcome"] == "exists":
                print(f"示例任务已存在：{task['task_id']}（幂等跳过）")
            else:
                print(
                    f"示例任务已建：{task['task_id']}"
                    f"（uploading→commit→queued，初始轮 {task['round_id']} 待领）"
                )
        _print_guidance()
        return 0
    finally:
        await engine.dispose()


async def run_async(argv: list[str] | None = None) -> int:
    """异步入口（测试 / 嵌入场景）：参数解析、防呆守卫与 main 完全同链。"""
    args = _parse_args(argv)
    if not args.i_know_demo_bypasses_review:
        print(_DECLARATION)
        return 2
    print(_BANNER)
    return await _run(args)


def main(argv: list[str] | None = None) -> int:
    """CLI 同步入口。"""
    return asyncio.run(run_async(argv))


if __name__ == "__main__":
    raise SystemExit(main())
