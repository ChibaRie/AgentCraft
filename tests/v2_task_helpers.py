"""任务域测试共享助手（Phase 6 T3）：superuser 种子 + owner 事务包装。

纪律（对齐 Phase 6 计划「测试基建」节）：
- owner-RLS 表（users/experts/expert_revisions/revision_tools/tasks/task_files/
  task_messages/task_rounds/task_events）种子一律走 pg.engine（superuser）——
  app/admin 角色无 INSERT policy（42501）；revision_tools 冻结触发器对 GUC 未设
  的 superuser 上下文放行（0007）；
- 被测服务函数一律经真实 app 角色会话（owner_tx，GUC 已设）执行——禁 superuser
  直跑（会掩盖 RLS 空转）；
- 需要真实写盘的测试须显式传 tmp 根（TaskStorage(tmp_path)）——T3 服务函数本身
  零磁盘 I/O（物理删 task-storage 是调用方 post-commit 兜底），为 T4+ 预留惯例。
"""

import uuid as _uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_OWNER_TX_SQL = "SELECT app.set_current_owner(:uid)"


@asynccontextmanager
async def owner_tx(engine, owner_id: str) -> AsyncIterator[AsyncSession]:
    """角色引擎上的 owner 单事务：set_current_owner + 显式 begin；退出提交、异常
    回滚（服务函数不 begin 不 commit 的调用方纪律；等价于 owner_session 的事务
    语义，但接受任意角色引擎——并发用例需双引擎双会话）。"""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            await session.execute(text(_OWNER_TX_SQL), {"uid": str(owner_id)})
            yield session


async def seed_task_user(pg, email: str, **quota_overrides) -> str:
    """active 用户 + user_quotas（平台默认 5/3/1/1GiB，可逐项覆写）+
    user_quota_usage 零行；返回 user id（字符串 UUID）。"""
    quotas = {
        "max_daily_tasks": 5,
        "max_active_tasks": 3,
        "max_running_tasks": 1,
        "max_retained_storage_bytes": 1_073_741_824,
        **quota_overrides,
    }
    async with pg.engine.begin() as conn:
        uid = (
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status) "
                    "VALUES (gen_random_uuid(), :e, :h, 'user', 'active') RETURNING id"
                ),
                {"e": email, "h": "seed-not-a-login-hash"},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO user_quotas (user_id, max_daily_tasks, max_active_tasks, "
                "max_running_tasks, max_retained_storage_bytes) "
                "VALUES (:u, :d, :a, :r, :s) ON CONFLICT (user_id) DO NOTHING"
            ),
            {
                "u": uid,
                "d": quotas["max_daily_tasks"],
                "a": quotas["max_active_tasks"],
                "r": quotas["max_running_tasks"],
                "s": quotas["max_retained_storage_bytes"],
            },
        )
        await conn.execute(
            text(
                "INSERT INTO user_quota_usage (user_id, active_tasks, running_tasks, "
                "retained_storage_bytes) VALUES (:u, 0, 0, 0) "
                "ON CONFLICT (user_id) DO NOTHING"
            ),
            {"u": uid},
        )
    return str(uid)


async def seed_published_revision(
    pg,
    owner_id: str,
    *,
    tools: tuple[tuple[str, str], ...] = (("check_code_style", "1"), ("read_task_file", "1")),
) -> str:
    """expert(published) → revision(published) → revision_tools 全链（0007 冻结
    语义：任务期 revision_tools 恒定）；tools 须为 0002 种子目录中的 (tool_id,
    version)。返回 revision id。"""
    async with pg.engine.begin() as conn:
        expert_id = (
            await conn.execute(
                text(
                    "INSERT INTO experts (id, owner_id, status) "
                    "VALUES (gen_random_uuid(), :u, 'published') RETURNING id"
                ),
                {"u": owner_id},
            )
        ).scalar_one()
        revision_id = (
            await conn.execute(
                text(
                    "INSERT INTO expert_revisions (id, expert_id, owner_id, revision_no, "
                    "content_json, content_sha256, status) "
                    "VALUES (gen_random_uuid(), :x, :u, 1, '{}', :h, 'published') RETURNING id"
                ),
                {"x": expert_id, "u": owner_id, "h": "a" * 64},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE experts SET published_revision_id = :r WHERE id = :x"),
            {"r": revision_id, "x": expert_id},
        )
        for tool_id, version in tools:
            await conn.execute(
                text(
                    "INSERT INTO revision_tools (id, expert_revision_id, tool_id, version) "
                    "VALUES (gen_random_uuid(), :r, :t, :v)"
                ),
                {"r": revision_id, "t": tool_id, "v": version},
            )
    return str(revision_id)


async def seed_input_file(
    pg,
    owner_id: str,
    task_id: str,
    *,
    size_bytes: int,
    state: str = "staged",
    direction: str = "input",
    file_name: str = "a.txt",
) -> str:
    """task_files 行（staged 默认；storage_key 逻辑形态 tasks/<task>/<uuid>）。
    返回 file id。"""
    # storage_key 在 Python 侧拼装：同一 :t 绑定既入 uuid 列又入串拼接会触发
    # asyncpg AmbiguousParameterError（text versus uuid）
    storage_key = f"tasks/{task_id}/{_uuid.uuid4()}"
    async with pg.engine.begin() as conn:
        fid = (
            await conn.execute(
                text(
                    "INSERT INTO task_files (id, task_id, owner_id, direction, file_name, "
                    "storage_key, sha256, size_bytes, state) "
                    "VALUES (gen_random_uuid(), :t, :u, :d, :n, :k, :h, :s, :st) "
                    "RETURNING id"
                ),
                {
                    "t": task_id,
                    "u": owner_id,
                    "d": direction,
                    "n": file_name,
                    "k": storage_key,
                    "h": "b" * 64,
                    "s": size_bytes,
                    "st": state,
                },
            )
        ).scalar_one()
    return str(fid)


async def seed_running_task(pg, uid: str, pid: str) -> str:
    """running 任务全套持有物（202 意图分支/删除全清用例的承载物）：

    seed_task_for_provider(status='queued') 为底（active+task_root reservation、
    active_tasks=1、user 消息、pending round）→ 轮 running（lease 三元组 +
    attempt=1）→ 任务 running → running reservation(held) + running_tasks=1 +
    platform_slots#1 租给本任务。返回 task id。
    """
    from tests.v2_provider_helpers import seed_task_for_provider

    tid = await seed_task_for_provider(pg, uid, pid, status="queued")
    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE task_rounds SET state = 'running', lease_owner = 'exec-test', "
                "lease_epoch = 1, lease_expires_at = now() + interval '90 seconds', "
                "attempt = 1 WHERE task_id = :t"
            ),
            {"t": tid},
        )
        await conn.execute(text("UPDATE tasks SET status = 'running' WHERE id = :t"), {"t": tid})
        await conn.execute(
            text(
                "INSERT INTO task_reservations (id, task_id, user_id, kind, bytes, state) "
                "VALUES (gen_random_uuid(), :t, :u, 'running', 0, 'held')"
            ),
            {"t": tid, "u": uid},
        )
        await conn.execute(
            text(
                "UPDATE user_quota_usage SET running_tasks = running_tasks + 1 WHERE user_id = :u"
            ),
            {"u": uid},
        )
        await conn.execute(
            text(
                "UPDATE platform_slots SET state = 'leased', task_id = :t, "
                "leased_until = now() + interval '90 seconds' "
                "WHERE slot_no = 1 AND state = 'free'"
            ),
            {"t": tid},
        )
    return str(tid)


# ---------------------------------------------------------------------------
# API 面共享助手（Phase 9 T6 前置）：原 test_v2_tasks_api 私有助手，
# 因 test_v2_task_sse 跨文件 import 而统一上移（拆分前的耦合收敛点）。
# ---------------------------------------------------------------------------

TASK_PASSWORD = "User-Passw0rd!"


@pytest.fixture
async def api_env(provider_env, tmp_path):
    """provider_env 基础上把任务物理存储根钉到 tmp（路由触盘用例统一入口）。"""
    from backend.v2.task_storage import TaskStorage

    provider_env.storage = TaskStorage(tmp_path / "task-storage")
    return provider_env


async def seed_login_domain(pg, email: str) -> tuple[str, str, str]:
    """登录态用户（真实密码哈希）+ 默认位 provider + published revision。"""
    from tests.v2_provider_helpers import seed_active_user, seed_provider

    uid = await seed_active_user(pg, email)
    pid = await seed_provider(pg, uid, is_default=True)
    rid = await seed_published_revision(pg, uid)
    return str(uid), str(pid), str(rid)


async def login_client(pg, email: str):
    """真实登录流（双 cookie + CSRF 头）客户端。"""
    from tests.v2_provider_helpers import auth_client, login

    client = auth_client()
    await login(client, email, TASK_PASSWORD)
    return client


async def create_task_request(
    client,
    rid: str,
    pid: str | None = None,
    *,
    idem: str = "t8a-create-1",
    initial: str = "第一句话",
):
    """POST /api/tasks 薄壳（provider_id 缺省即走默认位解析）。"""
    body = {"expert_revision_id": rid, "initial_message": initial}
    if pid is not None:
        body["provider_id"] = pid
    return await client.post("/api/tasks", json=body, headers={"Idempotency-Key": idem})
