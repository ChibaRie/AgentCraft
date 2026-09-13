"""演示数据种子（可选命令；幂等，存在即跳过）。

用法：
    # 写入 .env 指向的默认库
    python tools/seed_demo.py

    # 写入指定库（演示/截图用隔离库）
    python tools/seed_demo.py --database sqlite+aiosqlite:///./demo.db

内容：demo 专家用户（密码 secret123）+ 2 个已发布 Skill + 2 位已发布专家
+ 2 个示例任务。全部按用户名/名称键控：重复执行不产生重复数据；
不触碰已存在的其他账号。

纪律：默认不创建任何演示外数据。演示账号仅限本地开发环境使用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def _ensure_demo_user(db, created: list[str], skipped: list[str]):
    from sqlalchemy import select

    from backend.models.user import User
    from backend.services.user_service import hash_password

    demo = (await db.execute(select(User).where(User.username == "demo"))).scalar_one_or_none()
    if demo is not None:
        skipped.append("用户 demo")
        return demo
    demo = User(
        username="demo",
        email="demo@example.com",
        password_hash=hash_password("secret123"),
        role="expert",
    )
    db.add(demo)
    await db.flush()
    created.append("用户 demo（密码 secret123）")
    return demo


async def _ensure_skill(
    db,
    owner_id: int,
    created: list[str],
    skipped: list[str],
    *,
    name: str,
    use_case: str,
    role: str,
    goal: str,
    steps: str,
    output_requirements: str,
    constraints: str,
):
    from sqlalchemy import select

    from backend.models.skill import Skill

    row = (
        await db.execute(select(Skill).where(Skill.owner_id == owner_id, Skill.name == name))
    ).scalar_one_or_none()
    if row is not None:
        skipped.append(f"Skill {name}")
        return row
    row = Skill(
        owner_id=owner_id,
        name=name,
        description=f"{name}：可复用能力包",
        use_case=use_case,
        role=role,
        goal=goal,
        steps=steps,
        output_requirements=output_requirements,
        constraints=constraints,
        status="published",
    )
    db.add(row)
    await db.flush()
    created.append(f"Skill {name}（已发布）")
    return row


async def _ensure_expert(
    db,
    owner_id: int,
    created: list[str],
    skipped: list[str],
    *,
    name: str,
    description: str,
    persona: str,
    methodology: str,
    category: str,
    skill,
):
    from sqlalchemy import select

    from backend.models.expert import Expert
    from backend.models.expert_skill import ExpertSkill

    row = (
        await db.execute(select(Expert).where(Expert.owner_id == owner_id, Expert.name == name))
    ).scalar_one_or_none()
    if row is None:
        row = Expert(
            owner_id=owner_id,
            name=name,
            description=description,
            avatar_url=None,
            category=category,
            persona=persona,
            methodology=methodology,
            status="published",
        )
        db.add(row)
        await db.flush()
        created.append(f"专家 {name}（已发布）")
    else:
        skipped.append(f"专家 {name}")
    binding = (
        await db.execute(
            select(ExpertSkill).where(
                ExpertSkill.expert_id == row.id, ExpertSkill.skill_id == skill.id
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        db.add(ExpertSkill(expert_id=row.id, skill_id=skill.id, enabled=True))
    return row


async def _ensure_task(
    db,
    user_id: int,
    expert,
    title: str,
    status: str,
    created: list[str],
    skipped: list[str],
) -> None:
    from sqlalchemy import select

    from backend.models.conversation import Conversation
    from backend.models.message import Message
    from backend.models.task import Task

    row = (
        await db.execute(select(Task).where(Task.user_id == user_id, Task.title == title))
    ).scalar_one_or_none()
    if row is not None:
        skipped.append(f"任务 {title}")
        return
    now = datetime.now(timezone.utc)
    row = Task(
        user_id=user_id,
        expert_id=expert.id,
        expert_name_snapshot=expert.name,
        title=title,
        status=status,
        skill_snapshot=json.dumps({"skills": [], "loaded_at": now.isoformat()}, ensure_ascii=False),
        # 用户 MCP 面已下线（Phase 5 T2/D12）：快照恒为空工具集
        mcp_snapshot=json.dumps(
            {"tools": [], "loaded_at": now.isoformat()}, ensure_ascii=False
        ),
        provider_snapshot=json.dumps({"source": "system"}),
        workdir="/workspaces/authorized",
    )
    db.add(row)
    await db.flush()
    if status == "completed":
        conversation = Conversation(task_id=row.id)
        db.add(conversation)
        await db.flush()
        base = now - timedelta(hours=1)
        db.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content="请整理本周的技术周报要点。",
                created_at=base,
            )
        )
        db.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content="本周要点已按主题归类完成：共 3 个主题、2 条风险提示，详见正文。",
                created_at=base + timedelta(minutes=2),
            )
        )
    created.append(f"任务 {title}（{status}）")


async def seed(database_url: str | None) -> None:
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.models import Base

    url = database_url
    if url is None:
        from backend.config import get_settings

        url = get_settings().DATABASE_URL

    engine = create_async_engine(url, poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSessionAlias, expire_on_commit=False)
    created: list[str] = []
    skipped: list[str] = []

    async with session_factory() as db:
        # 1) demo 专家用户
        demo = await _ensure_demo_user(db, created, skipped)

        weekly = await _ensure_skill(
            db,
            demo.id,
            created,
            skipped,
            name="技术周报整理",
            use_case="团队每周技术动态汇总",
            role="技术编辑",
            goal="把一周的零散技术动态整理成结构化周报",
            steps="先收集素材并归类主题；再为每个主题提炼要点与影响；最后汇总风险提示",
            output_requirements="按主题分节的周报正文，末尾附三条风险提示",
            constraints="不虚构事实；引用需注明来源；不输出与工作无关内容",
        )
        minutes = await _ensure_skill(
            db,
            demo.id,
            created,
            skipped,
            name="会议纪要提炼",
            use_case="例会/评审的记录沉淀",
            role="会议秘书",
            goal="把冗长会议记录提炼为可执行的决议清单",
            steps="识别议题与结论；拆分行动项并标注责任人；汇总待决事项",
            output_requirements="决议清单 + 行动项表（责任人/截止日）",
            constraints="不添加会议中未出现的决议；存疑处标注待确认",
        )
        editor = await _ensure_expert(
            db,
            demo.id,
            created,
            skipped,
            name="周报编辑",
            description="整理团队技术周报的编辑专家",
            persona="严谨的资深技术编辑，擅长从零散素材中提炼主线",
            methodology="先收集素材，再按主题归类，最后输出结构化摘要与风险提示",
            category="tech",
            skill=weekly,
        )
        # 会议助手专家仅创建展示，无任务样例挂靠（原 MCP 绑定消费者已随 D12 删除）
        await _ensure_expert(
            db,
            demo.id,
            created,
            skipped,
            name="会议助手",
            description="把会议记录变成决议与行动项",
            persona="耐心细致的会议秘书，重视事实与责任人",
            methodology="先切议题，再提结论，最后核对行动项完整性",
            category="office",
            skill=minutes,
        )

        # 2) 示例任务（completed + created 各一，展示 P05/P09 视图）
        await _ensure_task(db, demo.id, editor, "整理本周技术周报", "completed", created, skipped)
        await _ensure_task(db, demo.id, editor, "起草月度技术回顾", "created", created, skipped)

        await db.commit()

    print("== 演示数据种子完成 ==")
    for item in created:
        print(f"  + {item}")
    for item in skipped:
        print(f"  = 已存在，跳过：{item}")
    print(f"  目标库：{url}")
    if created and any("demo" in item for item in created):
        print("  登录账号：demo / secret123（仅限本地开发环境）")
    await engine.dispose()


from sqlalchemy.ext.asyncio import AsyncSession as AsyncSessionAlias  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentCraft 演示数据种子（幂等）")
    parser.add_argument(
        "--database",
        default=None,
        help="SQLAlchemy 连接串；缺省用 .env 的 DATABASE_URL",
    )
    args = parser.parse_args()
    asyncio.run(seed(args.database))


if __name__ == "__main__":
    main()
