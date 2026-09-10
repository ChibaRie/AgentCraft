"""check_code_style Harness 测试（手册 §6.8 内部接口 + §7.4 扩展注册）。

- 路径解析：相对 /workspace 路径 → 任务 workdir 主机目录；拒绝绝对路径、
  `..` 越界与解析后逃逸（符号链接）；缺省为根
- ruff 执行：format --check + check 的产物解析为 issues；执行类失败（rc>=2、
  超时、ruff 缺失）→ 502
- 内部接口：X-Task-Token 三重校验复用；400 路径非法
- 扩展生成：task.ts 固定追加 check_code_style 注册块（§7.4）
"""

import asyncio
import json
from pathlib import Path

import pytest

from backend.config import Settings
from backend.engine.extension_generator import ExtensionGenerator
from backend.services import harness_service
from backend.services.harness_service import HarnessPathInvalidError

pytestmark = pytest.mark.asyncio


def make_settings(**overrides) -> Settings:
    return Settings(HOST_DATA_ROOT="./data", HOST_WORKSPACE_ROOT="./workspaces", **overrides)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    return root


async def test_resolve_defaults_to_root(tmp_path):
    root = make_root(tmp_path)
    assert await harness_service.resolve_workspace_path(root, None) == root
    assert await harness_service.resolve_workspace_path(root, "") == root


async def test_resolve_relative_ok(tmp_path):
    root = make_root(tmp_path)
    target = await harness_service.resolve_workspace_path(root, "pkg/mod.py")
    assert target == (root / "pkg" / "mod.py").resolve()


async def test_resolve_rejects_traversal(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(HarnessPathInvalidError):
        await harness_service.resolve_workspace_path(root, "../outside.py")
    with pytest.raises(HarnessPathInvalidError):
        await harness_service.resolve_workspace_path(root, "pkg/../../x.py")


async def test_resolve_rejects_absolute(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(HarnessPathInvalidError):
        await harness_service.resolve_workspace_path(root, "C:/Windows/system32")
    with pytest.raises(HarnessPathInvalidError):
        await harness_service.resolve_workspace_path(root, "/etc/passwd")


async def test_resolve_rejects_symlink_escape(tmp_path):
    root = make_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "pkg" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不允许创建符号链接")
    with pytest.raises(HarnessPathInvalidError):
        await harness_service.resolve_workspace_path(root, "pkg/link")


async def test_resolve_rejects_missing_path(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(HarnessPathInvalidError):
        await harness_service.resolve_workspace_path(root, "pkg/nope.py")


# ---------------------------------------------------------------------------
# ruff 执行与解析
# ---------------------------------------------------------------------------


class FakeRunner:
    def __init__(self, results) -> None:
        self.results = list(results)  # [(rc, stdout, stderr)]
        self.calls: list[tuple[list[str], Path]] = []

    async def __call__(self, argv, cwd):
        self.calls.append((argv, cwd))
        if not self.results:
            return (0, "", "")
        return self.results.pop(0)


async def test_run_ruff_clean(tmp_path):
    root = make_root(tmp_path)
    runner = FakeRunner([(0, "", ""), (0, "", "")])
    result = await harness_service.run_ruff_checks(root, runner=runner)
    assert result["passed"] is True
    assert result["issues"] == []
    assert runner.calls[0][0][:3] == ["ruff", "format", "--check"]
    assert runner.calls[1][0][:2] == ["ruff", "check"]


async def test_run_ruff_parses_issues(tmp_path):
    root = make_root(tmp_path)
    runner = FakeRunner(
        [
            (1, "would reformat: pkg/mod.py\n", ""),
            (1, "pkg/mod.py:1:8: F401 `os` imported but unused\n", ""),
        ]
    )
    result = await harness_service.run_ruff_checks(root, runner=runner)
    assert result["passed"] is False
    assert len(result["issues"]) == 2
    fmt_issue = result["issues"][0]
    assert fmt_issue["file"].endswith("pkg/mod.py")
    assert fmt_issue["code"] == "format"
    lint_issue = result["issues"][1]
    assert lint_issue["code"] == "F401"
    assert lint_issue["line"] == 1
    assert "imported but unused" in lint_issue["message"]


async def test_run_ruff_execution_error_maps_502(tmp_path):
    from backend.services.harness_service import RuffExecutionError

    root = make_root(tmp_path)
    runner = FakeRunner([(2, "", "error: no such file")])
    with pytest.raises(RuffExecutionError) as excinfo:
        await harness_service.run_ruff_checks(root, runner=runner)
    assert excinfo.value.status_code == 502


async def test_run_ruff_timeout_maps_502(tmp_path):
    from backend.services.harness_service import RuffExecutionError

    class SlowRunner:
        async def __call__(self, argv, cwd):
            await asyncio.sleep(10)
            return (0, "", "")

    root = make_root(tmp_path)
    with pytest.raises(RuffExecutionError):
        await harness_service.run_ruff_checks(root, runner=SlowRunner(), timeout=0.05)


# ---------------------------------------------------------------------------
# 扩展注册（§7.4 固定追加块）
# ---------------------------------------------------------------------------


def test_extension_registers_check_code_style(tmp_path):
    generator = ExtensionGenerator(tmp_path / "extensions")
    path = generator.generate(7, [], provider="faux")
    source = path.read_text(encoding="utf-8")
    assert "check_code_style" in source
    assert "/internal/harness/check-code-style" in source
    # 非 faux 同样注册
    path2 = generator.generate(8, [], provider="openai")
    assert "check_code_style" in path2.read_text(encoding="utf-8")


def test_extension_mcp_tools_and_harness_coexist(tmp_path):
    generator = ExtensionGenerator(tmp_path / "extensions")
    tools = [
        {
            "name": "list_directory",
            "label": "list_directory",
            "description": "d",
            "schema": {"type": "object"},
            "serverId": 1,
        }
    ]
    path = generator.generate(9, tools, provider="openai")
    source = path.read_text(encoding="utf-8")
    parsed = json.dumps(
        {"has_mcp": "list_directory" in source, "has_harness": "check_code_style" in source}
    )
    assert json.loads(parsed) == {"has_mcp": True, "has_harness": True}


def test_extension_declares_image_input_for_real_provider(tmp_path):
    """BYOK 模型必须声明图像输入（2026-09-09 事故：input:["text"] 硬编码使
    Pi read 工具剥离图像块——read.js getNonVisionImageNote 依据模型元数据
    input 是否含 "image" 决定丢弃，多模态模型被注册元数据冤枉）。"""
    generator = ExtensionGenerator(tmp_path / "extensions")
    source = generator.generate(11, [], provider="openai").read_text(encoding="utf-8")
    assert 'input: ["text", "image"]' in source
    # faux 回显引擎仍为纯文本（不涉及图像）
    faux = generator.generate(12, [], provider="faux").read_text(encoding="utf-8")
    assert 'input: ["text"]' in faux


# ---------------------------------------------------------------------------
# 内部接口（/internal/harness/check-code-style）
# ---------------------------------------------------------------------------


class FakeManager:
    def __init__(self, token: str, root: Path) -> None:
        self._token = token
        self._root = root

    def get_task_token(self, task_id: int):
        return self._token

    def resolve_workdir_host(self, stored_workdir: str) -> Path:
        return self._root


async def test_harness_endpoint_roundtrip(tmp_path, monkeypatch):
    import base64
    import os

    from fastapi.testclient import TestClient

    # 独立 DB + 临时工作区
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from backend.config import Settings, get_settings
    from backend.database import get_db
    from backend.dependencies import get_pi_engine_manager
    from backend.main import app
    from backend.models import Base
    from backend.services.task_token import create_task_token

    db_file = tmp_path / "h.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSessionAlias, expire_on_commit=False)

    async with session_factory() as session:
        from backend.models.task import Task

        session.add(
            Task(
                user_id=1,
                expert_id=1,
                expert_name_snapshot="e",
                title="t",
                status="running",
                skill_snapshot="{}",
                mcp_snapshot="{}",
                provider_snapshot="{}",
                workdir="/workspaces/authorized",
            )
        )
        await session.commit()

    workdir = tmp_path / "ws"
    workdir.mkdir()
    (workdir / "bad.py").write_text("import os" + chr(10) + "x =  1" + chr(10), encoding="utf-8")

    key = os.urandom(32)
    raw = base64.urlsafe_b64encode(key).decode().rstrip("=")
    settings = Settings(
        MCP_ENCRYPTION_ACTIVE_KID="primary",
        MCP_ENCRYPTION_KEYRING=f"primary:{raw}",
        HOST_DATA_ROOT=str(tmp_path / "data"),
        HOST_WORKSPACE_ROOT=str(tmp_path / "w2"),
    )
    token = create_task_token(1, instance="inst", model_id="m")
    manager = FakeManager(token, workdir)
    app.dependency_overrides[get_pi_engine_manager] = lambda: manager
    app.dependency_overrides[get_settings] = lambda: settings

    async def override_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            ok = client.post(
                "/internal/harness/check-code-style",
                json={"task_id": 1, "path": "bad.py"},
                headers={"X-Task-Token": token},
            )
            assert ok.status_code == 200, ok.text
            data = ok.json()["data"]
            assert data["passed"] is False
            assert any(i["file"].endswith("bad.py") for i in data["issues"])

            bad_path = client.post(
                "/internal/harness/check-code-style",
                json={"task_id": 1, "path": "../escape.py"},
                headers={"X-Task-Token": token},
            )
            assert bad_path.status_code == 400

            no_token = client.post("/internal/harness/check-code-style", json={"task_id": 1})
            assert no_token.status_code == 401
    finally:
        app.dependency_overrides.pop(get_pi_engine_manager, None)
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


from sqlalchemy.ext.asyncio import AsyncSession as AsyncSessionAlias  # noqa: E402
