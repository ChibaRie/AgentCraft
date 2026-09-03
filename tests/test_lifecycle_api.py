"""任务生命周期 API 测试（手册 §7.2.1 锁语义表、§7.8、PRD §4.5.4/§4.5.6）。

- complete：无锁预检（活动轮→request_abort 绕锁）→ 等待并持有 mutation
  lock → 持锁复验 running → completed + 回收容器；created/failed/completed 409
- delete：持锁（round→data 顺序与发送一致防死锁）→ 先停容器 → 级联删
  会话/消息/文件元数据/上传目录/扩展文件，不动项目目录
- abort：仅 running 且存在活动轮可中止（否则 409），202 保持 running
- 专家下架：running 任务原子置 completed + 回收容器（§7.8）
"""

from pathlib import Path

import pytest

from backend.dependencies import get_pi_engine_manager
from backend.main import app
from backend.services.task_locks import task_round_lock
from tests.conftest import make_scripted_manager
from tests.test_skills import auth_header, register_expert
from tests.test_tasks import make_published_expert

pytestmark = pytest.mark.usefixtures("client")


@pytest.fixture()
def lifecycle_env(test_db, tmp_path: Path, client):
    """脚本化假引擎 + 已登录专家用户（published 专家）+ 可播种任务/会话/消息。

    get_settings 覆盖到 tmp 目录：删除端点的文件清理断言与之一致。
    """
    from backend.config import Settings, get_settings

    manager, transports = make_scripted_manager(tmp_path, round_timeout=120)
    app.dependency_overrides[get_pi_engine_manager] = lambda: manager
    settings = Settings(
        HOST_DATA_ROOT=str(tmp_path / "data"),
        HOST_WORKSPACE_ROOT=str(tmp_path / "workspaces"),
    )
    (tmp_path / "workspaces").mkdir(parents=True, exist_ok=True)
    app.dependency_overrides[get_settings] = lambda: settings

    token, user_id, expert_id, _skill_id = make_published_expert(client, username="lc")
    expert = {"id": expert_id}

    def seed_task(status: str = "running", with_message: bool = True) -> int:
        created = client.post(
            "/api/tasks",
            json={"expert_id": expert["id"], "description": "生命周期测试任务"},
            headers=auth_header(token),
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["data"]["task_id"]
        if with_message:
            sent = client.post(
                f"/api/tasks/{task_id}/messages",
                json={"content": "hi"},
                headers=auth_header(token),
            )
            assert sent.status_code == 200
            assert b"event: done" in sent.content  # 消费整轮 SSE 至 done
        if status != "running":
            _set_status(test_db, task_id, status)
        return task_id

    yield LifecycleEnv(client, manager, transports, token, seed_task, test_db, tmp_path)
    app.dependency_overrides.pop(get_pi_engine_manager, None)
    app.dependency_overrides.pop(get_settings, None)


def _set_status(test_db, task_id: int, status: str) -> None:
    import asyncio

    from sqlalchemy import update

    from backend.models.task import Task

    async def run():
        async with test_db.session_factory() as session:
            await session.execute(
                update(Task).where(Task.id == task_id).values(status=status)
            )
            await session.commit()

    asyncio.run(run())


async def _seed_task_row(test_db) -> int:
    """服务级测试播种：user+expert+task（running）。"""
    from backend.models.expert import Expert
    from backend.models.task import Task
    from backend.models.user import User

    async with test_db.session_factory() as session:
        user = User(username="lc-svc", email="lc-svc@example.com",
                    password_hash="x", role="expert")
        session.add(user)
        await session.flush()
        expert = Expert(owner_id=user.id, name="svc专家", description="d" * 10,
                        category="tech", persona="p" * 10, methodology="m" * 10)
        session.add(expert)
        await session.flush()
        task = Task(
            user_id=user.id, expert_id=expert.id, expert_name_snapshot="svc专家",
            title="t", status="running", skill_snapshot="{}", mcp_snapshot="{}",
            provider_snapshot="{}", workdir="/workspaces/authorized",
        )
        session.add(task)
        await session.commit()
        return task.id


class LifecycleEnv:
    def __init__(self, client, manager, transports, token, seed_task, test_db, tmp_path):
        self.client = client
        self.manager = manager
        self.transports = transports
        self.token = token
        self.seed_task = seed_task
        self.test_db = test_db
        self.tmp_path = tmp_path

    def auth(self):
        return auth_header(self.token)

    def complete(self, task_id):
        return self.client.post(f"/api/tasks/{task_id}/complete", headers=self.auth())

    def delete(self, task_id):
        return self.client.delete(f"/api/tasks/{task_id}", headers=self.auth())

    def abort(self, task_id):
        return self.client.post(f"/api/tasks/{task_id}/abort", headers=self.auth())

    def task_status(self, task_id) -> str:
        response = self.client.get(f"/api/tasks/{task_id}", headers=self.auth())
        return response.json()["data"]["status"]

    def message_count(self, task_id) -> int:
        response = self.client.get(f"/api/tasks/{task_id}", headers=self.auth())
        return len(response.json()["data"]["messages"])


# ---------------------------------------------------------------------------
# complete（§7.2.1 结束语义）
# ---------------------------------------------------------------------------


def test_complete_running_task(lifecycle_env):
    env = lifecycle_env
    task_id = env.seed_task()  # 发送消息 → running，容器已由假引擎创建
    response = env.complete(task_id)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "completed"
    assert env.task_status(task_id) == "completed"
    # 容器回收 + 令牌失效
    assert env.manager._containers.get(task_id) is None
    assert env.manager.get_task_token(task_id) is None
    # completed 不可再发送
    sent = env.client.post(
        f"/api/tasks/{task_id}/messages",
        json={"content": "again"},
        headers=env.auth(),
    )
    assert sent.status_code == 409


def test_complete_created_task_409(lifecycle_env):
    env = lifecycle_env
    task_id = env.seed_task(with_message=False)  # created
    response = env.complete(task_id)
    assert response.status_code == 409
    assert env.task_status(task_id) == "created"


def test_complete_failed_task_409(lifecycle_env):
    env = lifecycle_env
    task_id = env.seed_task(status="failed")
    assert env.complete(task_id).status_code == 409


def test_complete_twice_409(lifecycle_env):
    env = lifecycle_env
    task_id = env.seed_task()
    assert env.complete(task_id).status_code == 200
    assert env.complete(task_id).status_code == 409


def test_complete_foreign_task_404(lifecycle_env):
    env = lifecycle_env
    task_id = env.seed_task()
    other_token, _ = register_expert(
        env.client, username="other-lc", email="other-lc@example.com"
    )
    response = env.client.post(
        f"/api/tasks/{task_id}/complete", headers=auth_header(other_token)
    )
    assert response.status_code == 403  # 任务侧所有权口径：非本人 403


async def test_complete_waits_active_round_then_completes(test_db, tmp_path):
    """存在活动轮：complete 无锁预检发现活动轮 → request_abort → 等锁 →
    持锁置 completed（服务级异步测试：真实锁语义）。"""
    import asyncio

    from backend.services import task_lifecycle

    factory = test_db.session_factory
    task_id = await _seed_task_row(test_db)

    manager = FakeLifecycleManager(active=True)
    lock = task_round_lock(task_id)
    await lock.acquire()  # 模拟活动轮持有 mutation lock

    async with factory() as session:
        coro = asyncio.create_task(
            task_lifecycle.complete_task(session, 1, task_id, manager)
        )
        await asyncio.sleep(0.05)
        assert not coro.done(), "锁被持有时 complete 必须等待"
        assert manager.aborted == [task_id], "持锁等待前先绕锁 request_abort"
        lock.release()
        task = await coro
        assert task.status == "completed"
        assert manager.stopped == [task_id], "完成后回收容器"


# ---------------------------------------------------------------------------
# delete（§7.2.1 删除语义 / PRD 4.5.6 删除约束）
# ---------------------------------------------------------------------------


def test_delete_task_cascades(lifecycle_env, tmp_path: Path):
    env = lifecycle_env
    task_id = env.seed_task()
    # 建上传目录与扩展文件占位
    upload_dir = tmp_path / "data" / "task-files" / f"task-{task_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "a.txt").write_text("x", encoding="utf-8")
    ext_file = tmp_path / "data" / "extensions" / f"task-{task_id}.ts"
    ext_file.parent.mkdir(parents=True, exist_ok=True)
    ext_file.write_text("// ext", encoding="utf-8")

    response = env.delete(task_id)
    assert response.status_code == 200
    assert response.json()["data"]["message"] == "deleted"
    assert env.manager._containers.get(task_id) is None
    # DB 级联（任务不存在 → 详情 404）
    detail = env.client.get(f"/api/tasks/{task_id}", headers=env.auth())
    assert detail.status_code == 404
    # 文件清理
    assert not upload_dir.exists()
    assert not ext_file.exists()


def test_delete_foreign_task_404(lifecycle_env):
    env = lifecycle_env
    task_id = env.seed_task()
    other_token, _ = register_expert(
        env.client, username="other-lc2", email="other-lc2@example.com"
    )
    response = env.client.delete(f"/api/tasks/{task_id}", headers=auth_header(other_token))
    assert response.status_code == 403


async def test_delete_waits_active_round(test_db, tmp_path):
    """删除持锁（§7.2.1）：锁被活动轮持有时等待，释放后先停容器再删数据。"""
    import asyncio

    from backend.services import task_lifecycle

    factory = test_db.session_factory
    task_id = await _seed_task_row(test_db)

    upload_dir = tmp_path / "data" / "task-files" / f"task-{task_id}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    ext_file = tmp_path / "data" / "extensions" / f"task-{task_id}.ts"
    ext_file.parent.mkdir(parents=True, exist_ok=True)
    ext_file.write_text("//", encoding="utf-8")

    manager = FakeLifecycleManager(active=False)
    lock = task_round_lock(task_id)
    await lock.acquire()

    async with factory() as session:
        coro = asyncio.create_task(
            task_lifecycle.delete_task(
                session, 1, task_id, manager,
                task_files_root=tmp_path / "data" / "task-files",
                extensions_root=tmp_path / "data" / "extensions",
            )
        )
        await asyncio.sleep(0.05)
        assert not coro.done(), "删除必须等待 mutation lock"
        lock.release()
        await coro
        assert manager.stopped == [task_id]
        assert not upload_dir.exists() and not ext_file.exists()


class FakeLifecycleManager:
    """complete/delete 锁语义测试用管理器替身（记录绕锁 abort 与容器回收）。"""

    def __init__(self, *, active: bool) -> None:
        self.active = active
        self.aborted: list[int] = []
        self.stopped: list[int] = []

    def has_active_round(self, task_id: int) -> bool:
        return self.active

    async def request_abort(self, task_id: int) -> None:
        self.aborted.append(task_id)

    async def stop_container(self, task_id: int) -> None:
        self.stopped.append(task_id)
        self.active = False


# ---------------------------------------------------------------------------
# abort 细化（PRD 4.5.6 中止约束）
# ---------------------------------------------------------------------------


def test_abort_without_active_round_409(lifecycle_env):
    env = lifecycle_env
    task_id = env.seed_task()
    # 上一轮已结束（seed_task 消费到 done）→ 无活动轮
    response = env.abort(task_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_NO_ACTIVE_ROUND"
    assert env.task_status(task_id) == "running"  # 中止不改终态


def test_abort_active_round_202(lifecycle_env):
    env = lifecycle_env
    task_id = env.seed_task()
    aborted: list[int] = []
    env.manager.has_active_round = lambda tid: True  # 实例级替换：模拟活动轮
    async def fake_abort(tid: int) -> None:
        aborted.append(tid)

    env.manager.request_abort = fake_abort  # type: ignore[method-assign]

    response = env.abort(task_id)
    assert response.status_code == 202
    assert response.json()["data"]["abort_requested"] is True
    assert aborted == [task_id]
    assert env.task_status(task_id) == "running"  # 中止不改终态


# ---------------------------------------------------------------------------
# 专家下架联动（§7.8）
# ---------------------------------------------------------------------------


def test_expert_offline_completes_running_tasks(lifecycle_env):
    env = lifecycle_env
    task_id = env.seed_task()
    # 专家 id=1（register_expert 首个专家）
    response = env.client.post("/api/experts/1/offline", headers=env.auth())
    assert response.status_code == 200
    assert env.task_status(task_id) == "completed"
    assert env.manager._containers.get(task_id) is None
    # 已 completed 的任务不受影响、不可再发送
    sent = env.client.post(
        f"/api/tasks/{task_id}/messages", json={"content": "x"}, headers=env.auth()
    )
    assert sent.status_code == 409
