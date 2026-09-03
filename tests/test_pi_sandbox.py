"""Pi 任务沙箱隔离性测试（手册 §7.2 沙箱清单、§7.9 安全边界）。

ContainerSpec 是容器创建参数的唯一事实源；to_cli_config() 与
to_api_kwargs() 两条通道必须呈现同一安全清单，不因通道漂移：

- 非 root（piworker）+ 只读 rootfs + cap-drop ALL + no-new-privileges
- 仅 internal 网络（无外网路由）、资源限额（512MB / 1 CPU）
- 可写点仅 /tmp 与 ~/.pi（tmpfs，随容器销毁）
- 挂载面仅三处：workdir rw、task-files ro、扩展文件 ro；无控制面数据、
  Docker socket 或其他任务目录；source 全部绝对路径
- env 只含任务令牌（可撤销的受限凭证），无任何原始 Provider Key
- workdir 主机路径解析拒绝越界（§7.9 路径校验）
"""

from pathlib import Path

import pytest

from backend.config import Settings
from backend.engine.docker_transport import build_container_spec
from backend.engine.pi_engine_manager import EngineStateError, PiEngineManager

_TASK_ID = 42
_TOKEN = "task-token-for-isolation-test"


def make_spec(tmp_path: Path, *, provider: str = "openai"):
    workdir = tmp_path / "authorized" / "proj"
    task_files = tmp_path / "data" / "task-files" / f"task-{_TASK_ID}"
    extension = tmp_path / "data" / "extensions" / f"task-{_TASK_ID}.ts"
    workdir.mkdir(parents=True, exist_ok=True)
    task_files.mkdir(parents=True, exist_ok=True)
    extension.parent.mkdir(parents=True, exist_ok=True)
    extension.write_text("// ext", encoding="utf-8")
    return build_container_spec(
        task_id=_TASK_ID,
        image="agentcraft-pi-worker:0.84.3",
        provider=provider,
        model="deepseek-chat",
        system_prompt="你是测试专家",
        workdir_host=workdir,
        task_files_host=task_files,
        extension_host=extension,
        task_token=_TOKEN,
        backend_url="http://agentcraft-control:8000",
        network_name="agentcraft-internal",
    )


def make_settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        HOST_DATA_ROOT=str(tmp_path / "data"),
        HOST_WORKSPACE_ROOT=str(tmp_path / "authorized"),
        PI_WORKER_IMAGE="agentcraft-pi-worker:0.84.3",
        **overrides,
    )


# ---------------------------------------------------------------------------
# 容器身份与加固（两通道一致）
# ---------------------------------------------------------------------------


def test_runs_as_non_root_user(tmp_path):
    spec = make_spec(tmp_path)
    assert spec.user == "piworker"
    assert "--user" in spec.to_cli_config()["pre_args"]
    assert spec.to_api_kwargs()["User"] == "piworker"


def test_readonly_rootfs_both_channels(tmp_path):
    spec = make_spec(tmp_path)
    assert "--read-only" in spec.to_cli_config()["pre_args"]
    assert spec.to_api_kwargs()["HostConfig"]["ReadonlyRootfs"] is True


def test_capabilities_dropped_and_no_new_privileges(tmp_path):
    spec = make_spec(tmp_path)
    pre_args = spec.to_cli_config()["pre_args"]
    assert "ALL" in pre_args and pre_args[pre_args.index("--cap-drop") + 1] == "ALL"
    no_new = pre_args[pre_args.index("--security-opt") + 1]
    assert no_new == "no-new-privileges"
    host = spec.to_api_kwargs()["HostConfig"]
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges"]


def test_internal_network_only(tmp_path):
    spec = make_spec(tmp_path)
    assert spec.network_name == "agentcraft-internal"
    pre_args = spec.to_cli_config()["pre_args"]
    assert pre_args[pre_args.index("--network") + 1] == "agentcraft-internal"
    assert spec.to_api_kwargs()["HostConfig"]["NetworkMode"] == "agentcraft-internal"


def test_resource_limits(tmp_path):
    spec = make_spec(tmp_path)
    assert spec.memory_bytes == 512 * 1024 * 1024
    assert spec.nano_cpus == 1_000_000_000
    pre_args = spec.to_cli_config()["pre_args"]
    assert pre_args[pre_args.index("--memory") + 1] == str(spec.memory_bytes)
    host = spec.to_api_kwargs()["HostConfig"]
    assert host["Memory"] == spec.memory_bytes
    assert host["NanoCpus"] == spec.nano_cpus


def test_tmpfs_writable_points_only(tmp_path):
    spec = make_spec(tmp_path)
    assert set(spec.tmpfs) == {"/tmp", "/home/piworker/.pi"}
    pre_args = spec.to_cli_config()["pre_args"]
    tmpfs_flags = [a for a in pre_args if a.startswith("--tmpfs=")]
    assert sorted(flag.split("=")[0] for flag in tmpfs_flags) == ["--tmpfs", "--tmpfs"]
    host = spec.to_api_kwargs()["HostConfig"]["Tmpfs"]
    assert set(host) == {"/tmp", "/home/piworker/.pi"}


# ---------------------------------------------------------------------------
# 挂载面（§7.9：仅三个派生源；无控制面/Docker socket/其他任务目录）
# ---------------------------------------------------------------------------


def test_mount_surface_is_exactly_three_derived_paths(tmp_path):
    spec = make_spec(tmp_path)
    mounts = spec.mounts
    assert len(mounts) == 3
    targets = {target for _, target, _ in mounts}
    assert targets == {"/workspace", "/task-files", "/extension/task.ts"}
    modes = {target: mode for _, target, mode in mounts}
    assert modes["/workspace"] == "rw"  # 任务工作区可写
    assert modes["/task-files"] == "ro"  # 上传文件只读
    assert modes["/extension/task.ts"] == "ro"  # 扩展只读
    # source 全部绝对路径且不含控制面敏感目录（data/extensions 之外无挂载）
    for source, _, _ in mounts:
        assert Path(source).is_absolute()
    joined = " ".join(source for source, _, _ in mounts)
    assert "docker.sock" not in joined
    assert "agentcraft.db" not in joined


def test_mount_flags_ro_rw_match_per_channel(tmp_path):
    spec = make_spec(tmp_path)
    pre_args = spec.to_cli_config()["pre_args"]
    cli_mounts = [a for a in pre_args if a.startswith("--mount=")]
    assert len(cli_mounts) == 3
    assert any("target=/task-files" in m and "readonly" in m for m in cli_mounts)
    assert any("target=/extension/task.ts" in m and "readonly" in m for m in cli_mounts)
    assert any("target=/workspace" in m and "readonly" not in m for m in cli_mounts)
    api_binds = spec.to_api_kwargs()["HostConfig"]["Binds"]
    assert len(api_binds) == 3
    assert any(bind.endswith(":ro") and "/task-files" in bind for bind in api_binds)
    assert any(bind.endswith(":/workspace:rw") for bind in api_binds)


# ---------------------------------------------------------------------------
# env 纪律（§7.9 凭证：Pi 只拿任务令牌，绝不拿原始 Key）
# ---------------------------------------------------------------------------


def test_env_carries_task_token_never_provider_key(tmp_path):
    spec = make_spec(tmp_path, provider="openai")
    assert spec.env["AGENTCRAFT_TASK_TOKEN"] == _TOKEN
    assert spec.env["OPENAI_API_KEY"] == _TOKEN  # proxy 路由用的任务令牌
    assert spec.env["OPENAI_BASE_URL"] == "http://provider-proxy:8080/v1"
    for key, value in spec.env.items():
        assert not value.startswith("sk-"), f"env {key} 疑似原始 Provider Key"
        assert "BEGIN PRIVATE KEY" not in value


def test_env_faux_has_no_upstream_credentials(tmp_path):
    spec = make_spec(tmp_path, provider="faux")
    assert "OPENAI_BASE_URL" not in spec.env
    assert "OPENAI_API_KEY" not in spec.env


def test_argv_is_shell_free_array(tmp_path):
    spec = make_spec(tmp_path)
    # argv 数组直传（Docker API）；CLI 通道 image 后同样是数组，不经 shell 拼接
    argv = spec.argv
    assert argv[0] == "pi"
    assert argv[argv.index("--mode") + 1] == "rpc"
    assert "--no-session" in argv
    assert argv[argv.index("-e") + 1] == "/extension/task.ts"
    assert spec.to_api_kwargs()["Cmd"] == argv
    assert spec.to_cli_config()["argv"] == argv
    assert "--system-prompt" in argv


# ---------------------------------------------------------------------------
# 路径校验（§7.9：workdir 解析拒绝越界）
# ---------------------------------------------------------------------------


def _manager_with(tmp_path: Path) -> PiEngineManager:
    from tests.conftest import make_scripted_manager

    manager, _transports = make_scripted_manager(tmp_path)
    return manager


def test_workdir_resolution_rejects_escape(tmp_path):
    manager = _manager_with(tmp_path)
    with pytest.raises(EngineStateError):
        manager.resolve_workdir_host("/workspaces/authorized/../../etc")
    # 规范内的子目录合法
    inside = manager.resolve_workdir_host("/workspaces/authorized/proj/sub")
    assert str(inside).startswith(str(Path(manager._settings.HOST_WORKSPACE_ROOT).resolve()))


def test_mount_sources_normalized_absolute(tmp_path):
    """§7.9：bind source 派生后必须绝对化（_absolute 归一化守卫）。"""
    from backend.engine.pi_engine_manager import PiEngineManager

    settings = make_settings(tmp_path)
    manager = PiEngineManager(
        settings,
        history_fetcher=None,
        provider_resolver=None,
        extension_generator=None,
    )
    resolved = manager._absolute(Path(settings.HOST_WORKSPACE_ROOT), "工作目录")
    assert resolved.is_absolute()
    assert Path(settings.HOST_WORKSPACE_ROOT).resolve() == resolved


# ---------------------------------------------------------------------------
# 扩展文件（能力上限快照）与任务标签
# ---------------------------------------------------------------------------


def test_extension_mounted_readonly_single_file(tmp_path):
    spec = make_spec(tmp_path)
    extension_mounts = [m for m in spec.mounts if m[1] == "/extension/task.ts"]
    assert len(extension_mounts) == 1
    source, _, mode = extension_mounts[0]
    assert mode == "ro"
    assert source.endswith(f"task-{_TASK_ID}.ts")


def test_task_label_carries_task_id(tmp_path):
    spec = make_spec(tmp_path)
    assert spec.labels == {"agentcraft.task_id": str(_TASK_ID)}
    assert f"--name pi-task-{_TASK_ID}" in " ".join(spec.to_cli_config()["pre_args"])
    assert spec.to_api_kwargs()["Labels"]["agentcraft.task_id"] == str(_TASK_ID)
