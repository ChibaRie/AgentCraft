"""seed_v2_demo 防呆三件套与演示链验收（Phase 8 T16；安全审查 I-3 硬验收）。

覆盖面（task-16-brief Step 1 TDD 前置）：
1. 缺 ``--i-know-demo-bypasses-review`` → 退出码 2 + 用途声明（先于一切 DB 连接）；
2. DSN host 非 localhost/127.0.0.1/::1 → 邀请/用户段放行、published 段拒绝直插
   （退出码 2，experts/skills 零行）；
3. 幂等：同库二跑全 V2 表快照逐表相等（email 键跳过，零行变更）；
4. token 红线：邀请 token 明文仅 stdout 打印、库内只有 SHA-256 哈希；
5. demo 用户激活走 API：脚本自身永不建 demo 用户行（激活后重跑补建示例任务链）。

前置：需要 V2 PostgreSQL（pg 夹具，模板库克隆）；pg.url() 的 host 必须为
localhost/127.0.0.1（CI 与本机 testcontainers 均满足），否则 published 段被
防呆守卫拒绝、用例 3/5 无法成立。
"""

import base64

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import tools.seed_v2_demo as seed_demo
from backend.v2.models import Base
from backend.v2.security import hash_token

_DEMO_EMAIL = "demo-t16@example.com"
_ADMIN_EMAIL = "admin-t16@example.com"
# b64url(32B)：测试 KEK（与 config 校验形态一致；neutralize_v2_env 置空后按需覆盖）
_TEST_KEK = base64.urlsafe_b64encode(b"\x01" * 32).decode().rstrip("=")


def _argv(pg_url: str, extra: list[str] | None = None) -> list[str]:
    return [
        "--dsn",
        pg_url,
        "--admin-email",
        _ADMIN_EMAIL,
        "--demo-email",
        _DEMO_EMAIL,
        "--admin-password",
        "admin-t16-password",
        "--demo-password",
        "demo-t16-password",
        "--i-know-demo-bypasses-review",
        *(extra or []),
    ]


async def _snapshot_all_tables(url: str) -> dict[str, list[dict]]:
    """全 V2 表快照（逐表 SELECT *，按全列排序保证确定性）。"""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            out: dict[str, list[dict]] = {}
            for table in Base.metadata.sorted_tables:
                order = ", ".join(f'"{c.name}"' for c in table.columns)
                rows = (
                    (await conn.execute(text(f'SELECT * FROM "{table.name}" ORDER BY {order}')))
                    .mappings()
                    .all()
                )
                out[table.name] = [dict(r) for r in rows]
            return out
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 防呆一：显式 bypass flag 前置
# ---------------------------------------------------------------------------


def test_missing_bypass_flag_exits_with_declaration(capsys):
    """未带 --i-know-demo-bypasses-review：退出码 2 + 用途声明，零 DB 触达。"""
    rc = seed_demo.main(
        [
            "--dsn",
            "postgresql+asyncpg://u:p@localhost:5432/db",
            "--admin-password",
            "x",
            "--demo-password",
            "y",
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "仅限本地开发" in out
    assert "--i-know-demo-bypasses-review" in out
    assert "绕过" in out  # 用途声明必须说明「直插/绕过审核」性质


def test_local_host_acceptance():
    """DSN host 判定纯函数：localhost/127.0.0.1/::1 放行，其余（含空）拒绝。"""
    for dsn in (
        "postgresql+asyncpg://u:p@localhost:5432/db",
        "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
        "postgresql+asyncpg://u:p@[::1]:5432/db",
        "postgresql+asyncpg://u:p@LOCALHOST/db",
    ):
        assert seed_demo._is_local_host(seed_demo._dsn_host(dsn)) is True, dsn
    for dsn in (
        "postgresql+asyncpg://u:p@10.1.2.3:5432/db",
        "postgresql+asyncpg://u:p@db.example.com:5432/db",
        "postgresql+asyncpg://u:p@203.0.113.9/db",
        "sqlite+aiosqlite:///./demo.db",  # 无 host 形态一律视为非本地（保守拒绝）
    ):
        assert seed_demo._is_local_host(seed_demo._dsn_host(dsn)) is False, dsn


# ---------------------------------------------------------------------------
# 防呆二：非 localhost DSN 拒绝直插 published 段（邀请/用户段放行）
# ---------------------------------------------------------------------------


async def test_non_local_dsn_runs_identity_segment_but_refuses_published(monkeypatch, pg, capsys):
    """host=203.0.113.9（TEST-NET-3）：admin/邀请段照常执行，published 段拒绝。

    admin 段桩化（真实 seed_admin 会按非本地 DSN 建引擎连接失败）；邀请段经
    _engine 桩重定向到真实测试库——验证「邀请/用户段放行」与「published 拒绝」
    的分段语义，退出码 2。走异步入口 run_async（与 main 守卫同链）。
    """
    recorded: list[str] = []

    async def _stub_admin(dsn: str, email: str, password: str) -> str:
        recorded.append(dsn)
        return "seeded"

    monkeypatch.setattr(seed_demo, "seed_admin", _stub_admin)
    monkeypatch.setattr(seed_demo, "_engine", lambda _dsn: create_async_engine(pg.url()))

    rc = await seed_demo.run_async(_argv("postgresql+asyncpg://u:p@203.0.113.9:5432/db"))
    assert rc == 2
    out = capsys.readouterr().out
    assert "拒绝" in out and "203.0.113.9" in out
    assert recorded == ["postgresql+asyncpg://u:p@203.0.113.9:5432/db"]  # 用户段放行实证

    engine = create_async_engine(pg.url())
    try:
        async with engine.connect() as conn:
            invitations = (
                (await conn.execute(text("SELECT count(*) FROM invitations"))).scalar_one(),
            )
            experts = (await conn.execute(text("SELECT count(*) FROM experts"))).scalar_one()
            skills = (await conn.execute(text("SELECT count(*) FROM skills"))).scalar_one()
        assert invitations[0] == 1  # 邀请段真实落库
        assert experts == 0  # published 直插被拒
        assert skills == 0
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 防呆三 + 演示链：幂等二跑零变化 / token 红线 / 激活走 API / 任务链
# ---------------------------------------------------------------------------


async def test_seed_idempotent_second_run_zero_change(pg, capsys):
    """同库二跑：全 V2 表快照逐表相等；admin 建行、demo 用户零行（激活走 API）。"""
    url = pg.url()
    first_rc = await seed_demo.run_async(_argv(url))
    assert first_rc == 0
    first = await _snapshot_all_tables(url)

    second_rc = await seed_demo.run_async(_argv(url))
    assert second_rc == 0
    second = await _snapshot_all_tables(url)

    assert first.keys() == second.keys()
    for name in first:
        assert first[name] == second[name], f"表 {name} 二跑发生变化"

    # demo 用户永不由脚本直插（激活走 API）：users 仅 admin 一行
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            users = (await conn.execute(text("SELECT email, role FROM users"))).all()
            assert [(e, r) for e, r in users] == [(_ADMIN_EMAIL, "admin")]
    finally:
        await engine.dispose()


async def test_invitation_token_plaintext_stdout_only_hash_in_db(pg, capsys):
    """token 红线：明文仅 stdout；invitations.token_hash = SHA-256(明文)。"""
    rc = await seed_demo.run_async(_argv(pg.url()))
    assert rc == 0
    out = capsys.readouterr().out
    marker = "邀请 token："
    assert marker in out, "stdout 须打印邀请 token 明文（仅一次）"
    token = out.split(marker, 1)[1].splitlines()[0].strip()
    assert token, "token 明文不得为空"

    engine = create_async_engine(pg.url())
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text("SELECT token_hash, email FROM invitations WHERE email = :e"),
                    {"e": _DEMO_EMAIL},
                )
            ).all()
        assert len(rows) == 1
        assert rows[0][0] == hash_token(token)
        assert rows[0][1] == _DEMO_EMAIL
        assert token not in repr(rows)  # 明文不落库
    finally:
        await engine.dispose()


async def test_demo_task_chain_after_api_activation(monkeypatch, pg):
    """demo 用户已激活（预置行模拟 API 激活产物）→ 重跑补建示例任务链。

    断言：faux 目录启用、published expert/skill 直插、user_provider 密文落库、
    任务经 create_task(uploading)+commit_input(→queued, pending 轮)全链落库；
    二跑任务数不变（幂等）。
    """
    monkeypatch.setenv("PROVIDER_KEY_ENCRYPTION_KEY", _TEST_KEK)
    url = pg.url()

    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status, "
                    "mfa_secret_enc) VALUES (gen_random_uuid(), :e, 'x', 'user', "
                    "'pending', NULL)"
                ),
                {"e": _DEMO_EMAIL},
            )
    finally:
        await engine.dispose()

    rc = await seed_demo.run_async(_argv(url))
    assert rc == 0

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            catalog = (
                await conn.execute(
                    text("SELECT enabled FROM provider_catalog WHERE allowed_host = 'faux.invalid'")
                )
            ).scalar_one()
            expert_rev = (
                await conn.execute(
                    text(
                        "SELECT r.id, r.status, e.published_revision_id "
                        "FROM experts e JOIN expert_revisions r ON r.expert_id = e.id "
                        "WHERE e.status = 'published'"
                    )
                )
            ).all()
            skill_rev = (
                await conn.execute(
                    text(
                        "SELECT r.status FROM skills s JOIN skill_revisions r "
                        "ON r.skill_id = s.id WHERE s.status = 'published'"
                    )
                )
            ).all()
            providers = (
                await conn.execute(text("SELECT count(*) FROM user_providers"))
            ).scalar_one()
            task = (
                await conn.execute(
                    text(
                        "SELECT status, input_committed_at, input_manifest_sha256, "
                        "initial_message_id FROM tasks"
                    )
                )
            ).one()
            rounds = (await conn.execute(text("SELECT state FROM task_rounds"))).scalars().all()
            events = (
                (await conn.execute(text("SELECT type FROM task_events ORDER BY sequence")))
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    assert catalog is True  # faux 目录仅 dev 启用
    assert len(expert_rev) == 1 and expert_rev[0][1] == "published"  # 绕审核直插
    assert expert_rev[0][0] == expert_rev[0][2]  # published 指针就位
    assert len(skill_rev) == 1 and skill_rev[0][0] == "published"
    assert providers == 1  # demo 用户 faux provider（密文经 KEK 封装）
    assert task[0] == "queued"  # uploading → commit_input → queued
    assert task[1] is not None and task[2] is not None
    assert task[3] is not None  # 初始消息回填
    assert rounds == ["pending"]  # 初始轮待领
    assert [e for e in events] == ["message_saved", "status_changed", "round_queued"]

    # 幂等复核：二跑任务数不变
    assert await seed_demo.run_async(_argv(url)) == 0
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            count = (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
    finally:
        await engine.dispose()
    assert count == 1


async def test_demo_task_chain_requires_kek(monkeypatch, pg, capsys):
    """demo 用户在册但 KEK 未配置 → 干净报错（退出码 2），不落半账。"""
    monkeypatch.setenv("PROVIDER_KEY_ENCRYPTION_KEY", "")
    url = pg.url()
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status, "
                    "mfa_secret_enc) VALUES (gen_random_uuid(), :e, 'x', 'user', "
                    "'pending', NULL)"
                ),
                {"e": _DEMO_EMAIL},
            )
    finally:
        await engine.dispose()

    rc = await seed_demo.run_async(_argv(url))
    assert rc == 2
    captured = capsys.readouterr()
    assert "PROVIDER_KEY_ENCRYPTION_KEY" in captured.out + captured.err
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM tasks"))).scalar_one() == 0
            assert (
                await conn.execute(text("SELECT count(*) FROM user_providers"))
            ).scalar_one() == 0
    finally:
        await engine.dispose()
