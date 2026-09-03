"""Docker 传输：任务容器创建参数 + 两种 attach 通道（§7.1/§7.2 沙箱清单）。

- `ContainerSpec`：容器名/argv/env/挂载/沙箱/网络的唯一事实源，
  `to_cli_config()` 与 `to_api_kwargs()` 分别供 CLI 与 API 通道使用，
  保证两条路径的安全清单不会漂移（非 root、只读 rootfs、cap_drop=ALL、
  no-new-privileges、/tmp tmpfs、资源限额、仅 internal 网络、labels）。
- `DockerApiTransport`：aiodocker attach 流（DOCKER_API_URL，TCP）——
  compose/生产形态（docker-socket-proxy）。
- `DockerCliTransport`：`docker run -i` 子进程 stdio attach——开发机
  Docker Desktop 只有 npipe、无 TCP 时的回退，沙箱语义与 API 通道一致。

挂载源全部服务端派生（§7.9）：workdir 主机目录、task-files 任务目录、
扩展单文件；绝不挂载控制面数据、Docker socket 或其他任务目录。
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("agentcraft")


@dataclass(frozen=True)
class ContainerSpec:
    """单个 Pi 任务容器的完整创建参数（§7.2 清单的唯一事实源）。"""

    container_name: str
    image: str
    # 容器内 argv（Docker API 数组直传，绝不经 shell 拼接，§7.3.6）
    argv: list[str]
    env: dict[str, str]
    # 主机路径 → 容器挂载（§7.2：仅三个）
    mounts: list[tuple[str, str, str]]  # (source, target, mode)
    network_name: str
    labels: dict[str, str]
    workdir: str = "/workspace"
    user: str = "piworker"
    memory_bytes: int = 512 * 1024 * 1024
    nano_cpus: int = 1_000_000_000  # 1 CPU

    def to_cli_config(self) -> dict:
        """`docker run` 参数（DockerCliTransport 用）。"""
        env_args: list[str] = []
        for key, value in self.env.items():
            env_args += ["-e", f"{key}={value}"]
        label_args: list[str] = []
        for key, value in self.labels.items():
            label_args += ["--label", f"{key}={value}"]
        return {
            "pre_args": [
                "run",
                "--name", self.container_name,
                "--rm",
                "-i",
                "--detach-keys", "",
                "--user", self.user,
                "--workdir", self.workdir,
                "--network", self.network_name,
                "--memory", str(self.memory_bytes),
                "--cpus", str(self.nano_cpus / 1_000_000_000),
                "--read-only",
                "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges",
                "--tmpfs", "/tmp:rw,size=64m,nosuid,nodev,noexec",
                *label_args,
                *env_args,
                *[
                    f"--mount=type=bind,src={src},target={tgt},{mode}"
                    for src, tgt, mode in self.mounts
                ],
            ],
            "image": self.image,
            "argv": self.argv,
            "summary": " ".join(shlex.quote(part) for part in [*self.argv]),
        }

    def to_api_kwargs(self) -> dict:
        """aiodocker containers.create/watch 参数（DockerApiTransport 用）。"""
        return {
            "name": self.container_name,
            "Image": self.image,
            "Cmd": self.argv,
            "Env": [f"{key}={value}" for key, value in self.env.items()],
            "WorkingDir": self.workdir,
            "User": self.user,
            "Labels": dict(self.labels),
            "AttachStdin": True,
            "AttachStdout": True,
            "AttachStderr": False,
            "OpenStdin": True,
            "StdinOnce": False,
            "Tty": False,
            "HostConfig": {
                "Binds": [f"{src}:{tgt}:{mode}" for src, tgt, mode in self.mounts],
                "NetworkMode": self.network_name,
                "Memory": self.memory_bytes,
                "NanoCpus": self.nano_cpus,
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"],
                "Tmpfs": {"/tmp": "rw,size=64m,nosuid,nodev,noexec"},
                "AutoRemove": False,
            },
        }


class DockerApiTransport:
    """aiodocker attach 流传输（write_line/readline/close）。"""

    def __init__(self, docker, spec: ContainerSpec) -> None:  # aiodocker.Docker
        self._docker = docker
        self._spec = spec
        self._container = None
        self._stream = None

    async def start(self) -> str:
        self._container = await self._docker.containers.create(**self._spec.to_api_kwargs())
        self._stream = await self._container.attach(
            stdin=True, stdout=True, stderr=False, stream=True
        )
        await self._container.start()
        logger.info("Pi 容器已启动 name=%s", self._spec.container_name)
        return self._container._id

    async def write_line(self, line: str) -> None:
        assert self._stream is not None
        await self._stream.write_in((line + "\n").encode("utf-8"))

    async def readline(self) -> str | None:
        assert self._stream is not None
        message = await self._stream.read_out()
        if message is None:
            return None
        return message.data.decode("utf-8").rstrip("\r\n")

    async def close(self) -> None:
        if self._stream is not None:
            try:
                await self._stream.close()
            except Exception:  # noqa: BLE001 - 关闭失败不阻塞清理
                pass
            self._stream = None


class DockerCliTransport:
    """`docker run -i` 子进程 stdio 传输（npipe 环境回退）。"""

    def __init__(self, spec: ContainerSpec, *, docker_bin: str = "docker") -> None:
        self._spec = spec
        self._docker_bin = docker_bin
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> str:
        config = self._spec.to_cli_config()
        argv = [self._docker_bin, *config["pre_args"], config["image"], *config["argv"]]
        logger.info("启动 Pi 容器（CLI）: %s", " ".join(config["pre_args"][:2]))
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return self._spec.container_name

    async def write_line(self, line: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((line + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def readline(self) -> str | None:
        if self._proc is None or self._proc.stdout is None:
            return None
        raw = await self._proc.stdout.readline()
        if not raw:
            return None
        return raw.decode("utf-8").rstrip("\r\n")

    async def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        self._proc = None


# ---------------------------------------------------------------------------
# 容器生命周期 CLI 助手（manager 的 stop_hook / 巡检使用）
# ---------------------------------------------------------------------------


async def docker_remove_container(name: str, *, docker_bin: str = "docker") -> None:
    """`docker rm -f` 尽力删除（容器不存在视为成功）。"""
    proc = await asyncio.create_subprocess_exec(
        docker_bin, "rm", "-f", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def docker_list_task_containers(
    label_filter: str, *, docker_bin: str = "docker"
) -> list[str]:
    """按 label 查找遗留任务容器（启动巡检用）。"""
    proc = await asyncio.create_subprocess_exec(
        docker_bin, "ps", "-q", "--filter", f"label={label_filter}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return [line for line in stdout.decode().split() if line]


def build_container_spec(
    *,
    task_id: int,
    image: str,
    provider: str,
    model: str,
    system_prompt: str,
    workdir_host: Path,
    task_files_host: Path,
    extension_host: Path,
    task_token: str,
    backend_url: str,
    network_name: str,
    faux_chunk_delay_ms: int = 0,
) -> ContainerSpec:
    """按 §7.2 清单组装容器规格（挂载源全部服务端派生）。"""
    argv = [
        "pi",
        "--mode", "rpc",
        "--no-session",
        "--system-prompt", system_prompt,
        "--approve",
        "--provider", provider,
        "--model", model,
        "-e", "/extension/task.ts",
    ]
    env = {
        "AGENTCRAFT_BACKEND_URL": backend_url,
        "AGENTCRAFT_TASK_TOKEN": task_token,
        "AGENTCRAFT_PROVIDER": provider,
        "NODE_ENV": "production",
    }
    if faux_chunk_delay_ms:
        env["AGENTCRAFT_FAUX_CHUNK_DELAY_MS"] = str(faux_chunk_delay_ms)
    if provider != "faux":
        # faux 不需要；openai 路径指向 provider-proxy，真实 Key 永不进容器（§7.7）
        env["OPENAI_BASE_URL"] = "http://provider-proxy:8080/v1"
        env["OPENAI_API_KEY"] = task_token
    return ContainerSpec(
        container_name=f"pi-task-{task_id}",
        image=image,
        argv=argv,
        env=env,
        mounts=[
            (str(workdir_host), "/workspace", "rw"),
            (str(task_files_host), "/task-files", "ro"),
            (str(extension_host), "/extension/task.ts", "ro"),
        ],
        network_name=network_name,
        labels={"agentcraft.task_id": str(task_id)},
    )
