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
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("agentcraft")

# JSONL 帧行上限（字节）。推导：任务文件单文件上限 20MiB（config.UPLOAD_MAX_FILE_BYTES）
# × base64 膨胀 4/3（≈27.3MiB）+ JSON 信封余量，取整 32MiB（§7.6）。
# 传输层 limit 与引擎层超长行防护（pi_engine）共用此常量，两层必须对齐——
# 2026-09-08 任务4事故：asyncio 默认 64KB 在传输层先抛 ValueError，
# 引擎防护从未生效，图像 tool_result 大帧致崩溃恢复 3 次耗尽。
MAX_LINE_BYTES = 32 * 1024 * 1024


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
    # 只读 rootfs 下的可写点：/tmp 常规临时；~/.pi 供 pi 凭证存储
    # （auth.json 含任务令牌，tmpfs 随容器销毁，不入镜像层）
    tmpfs: dict = field(default_factory=lambda: {
        "/tmp": "rw,size=64m,nosuid,nodev,noexec",
        "/home/piworker/.pi": "rw,size=16m,nosuid,nodev,noexec",
    })

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
                *[f"--tmpfs={target}:{opts}" for target, opts in sorted(self.tmpfs.items())],
                *label_args,
                *env_args,
                *[
                    # --mount 语法：只读用 readonly 标志（缺省即 rw）
                    (
                        f"--mount=type=bind,src={src},target={tgt},readonly"
                        if mode == "ro"
                        else f"--mount=type=bind,src={src},target={tgt}"
                    )
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
                "Tmpfs": dict(self.tmpfs),
                "AutoRemove": False,
            },
        }


class _LineAssembler:
    """把 aiodocker demux chunk 流还原为 JSONL 行（§7.6）。

    Docker stdout 帧按容器进程 write 边界分块（大行必拆、小 write 可合），
    按 '\n' 缓存切分逐行返回；EOF 残留尾巴一次性交出，不静默吞数据。
    """

    def __init__(self, read_out) -> None:  # read_out: 返回 .data(bytes)|None 的协程
        self._read_out = read_out
        self._buf = bytearray()
        self._eof = False

    async def readline(self) -> str | None:
        while b"\n" not in self._buf:
            if self._eof:
                if not self._buf:
                    return None
                tail = self._buf.decode("utf-8", "replace")
                self._buf.clear()
                return tail
            message = await self._read_out()
            if message is None:
                self._eof = True
                continue
            self._buf += message.data
        idx = self._buf.index(b"\n")
        line = bytes(self._buf[:idx])
        del self._buf[: idx + 1]
        return line.decode("utf-8").rstrip("\r")


class DockerApiTransport:
    """aiodocker attach 流传输（write_line/readline/close）。"""

    def __init__(self, docker, spec: ContainerSpec) -> None:  # aiodocker.Docker
        self._docker = docker
        self._spec = spec
        self._container = None
        self._stream = None
        self._assembler: _LineAssembler | None = None

    async def start(self) -> str:
        self._container = await self._docker.containers.create(**self._spec.to_api_kwargs())
        self._stream = await self._container.attach(
            stdin=True, stdout=True, stderr=False, stream=True
        )
        await self._container.start()
        self._assembler = _LineAssembler(self._stream.read_out)
        logger.info("Pi 容器已启动 name=%s", self._spec.container_name)
        return self._container._id

    async def write_line(self, line: str) -> None:
        assert self._stream is not None
        await self._stream.write_in((line + "\n").encode("utf-8"))

    async def readline(self) -> str | None:
        if self._assembler is None:
            return None
        return await self._assembler.readline()

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
        self._stderr_task: asyncio.Task | None = None

    async def start(self) -> str:
        config = self._spec.to_cli_config()
        argv = [self._docker_bin, *config["pre_args"], config["image"], *config["argv"]]
        logger.info("启动 Pi 容器（CLI）: %s", " ".join(config["pre_args"][:2]))
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_LINE_BYTES,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        return self._spec.container_name

    async def _drain_stderr(self) -> None:
        """容器 stderr 接入日志（§7.8：记录容器 stderr 诊断）。"""
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            raw = await self._proc.stderr.readline()
            if not raw:
                return
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                logger.warning("容器 %s stderr: %s", self._spec.container_name, text)

    async def write_line(self, line: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((line + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def readline(self) -> str | None:
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            raw = await self._proc.stdout.readline()
        except ValueError:
            # 行超过 limit（asyncio LimitOverrunError 包装）：超限帧整体丢弃（§7.6
            # 末道防线），排空残留字节直至分隔符，返回空行交引擎静默跳过
            # （pi_engine.handle_line 对空行直接 return），轮次继续至 agent_settled
            await self._drain_oversize_line()
            return ""
        if not raw:
            return None
        return raw.decode("utf-8").rstrip("\r\n")

    async def _drain_oversize_line(self) -> None:
        """排空超限行残留：LimitOverrunError 后数据仍在缓冲，读到分隔符或 EOF。"""
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            chunk = await self._proc.stdout.read(MAX_LINE_BYTES)
            if not chunk or chunk.endswith(b"\n"):
                return

    async def close(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 - 收割失败不阻塞清理
            pass


# ---------------------------------------------------------------------------
# 容器生命周期 CLI 助手（manager 的 stop_hook / 巡检使用）
# ---------------------------------------------------------------------------


async def docker_ensure_network(
    name: str, *, docker_bin: str = "docker"
) -> None:
    """确保 internal 任务网络存在（§7.2 网络：仅 control 与 provider-proxy）。"""
    proc = await asyncio.create_subprocess_exec(
        docker_bin, "network", "inspect", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await proc.wait() == 0:
        return
    create = await asyncio.create_subprocess_exec(
        docker_bin, "network", "create", "--internal", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await create.communicate()
    if create.returncode != 0:
        logger.warning("创建网络 %s 失败: %s", name, stderr.decode(errors="replace").strip())


async def docker_ensure_proxy_container(
    *,
    image: str,
    container_name: str,
    network_name: str,
    app_dir: Path,
    docker_bin: str = "docker",
) -> None:
    """确保 provider-proxy 容器运行（§7.7 唯一双网络服务）。

    - 双网络：默认 bridge（出公网到用户上游）+ agentcraft-internal（被 Pi 容器
      以 provider-proxy:8080 访问）
    - 业务代码与 .env 只读挂载（Key/密钥环不入镜像层）
    - 已存在则 start（崩溃自愈）；不存在则 create+connect+start
    """
    inspect = await asyncio.create_subprocess_exec(
        docker_bin, "inspect", container_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await inspect.wait() == 0:
        start = await asyncio.create_subprocess_exec(
            docker_bin, "start", container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await start.communicate()
        if start.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            logger.warning("启动 %s 失败: %s", container_name, detail)
        return

    env_file = app_dir / ".env"
    run = await asyncio.create_subprocess_exec(
        docker_bin, "run", "-d",
        "--name", container_name,
        "--network", network_name,
        "-v", f"{app_dir}:/app:ro",
        "-v", f"{env_file}:/app/.env:ro",
        image,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await run.communicate()
    if run.returncode != 0:
        logger.warning("创建 %s 失败: %s", container_name, stderr.decode(errors="replace").strip())
        return
    # 连接默认 bridge 获得出站公网能力（internal 网络无路由）
    connect = await asyncio.create_subprocess_exec(
        docker_bin, "network", "connect", "bridge", container_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await connect.communicate()
    if connect.returncode != 0:
        logger.warning(
            "proxy 出站网络连接失败（用户上游可能不可达）: %s",
            stderr.decode(errors="replace").strip(),
        )
    logger.info("Provider Proxy 容器已就绪: %s", container_name)


async def docker_remove_container(name: str, *, docker_bin: str = "docker") -> None:
    """`docker rm -f` 尽力删除（容器不存在视为成功）。"""
    proc = await asyncio.create_subprocess_exec(
        docker_bin, "rm", "-f", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


# 开发形态后端转发器（阶段 6 MCP 桥）：任务容器只入 internal 网络（§10.2），
# 而 /internal/mcp/call 需要容器回调控制面。开发机控制面跑在宿主机时，
# 用一个 双网络（internal+bridge）转发容器 在 internal 网络内以
# `agentcraft-control` 别名监听并转发到 host.docker.internal:<port>——
# 与 provider-proxy 同款双网络模式；compose 形态（存在 agentcraft-control
# 容器）自动跳过。Pi 容器网络隔离不变。
_FORWARDER_NAME = "agentcraft-control-forwarder"

# node TCP 转发（pi-worker 镜像自带 node，免新增镜像）
_FORWARDER_SCRIPT = (
    "const net=require('net');const HOST=process.env.FWD_HOST||'host.docker.internal';"
    "const PORT=Number(process.env.FWD_PORT||8000);"
    "net.createServer((client)=>{const up=net.connect(PORT,HOST);"
    "client.pipe(up);up.pipe(client);"
    "client.on('error',()=>up.destroy());up.on('error',()=>client.destroy());"
    "}).listen(PORT,'0.0.0.0');"
)


async def docker_ensure_backend_forwarder(
    *,
    network_name: str,
    image: str,
    target_port: int,
    docker_bin: str = "docker",
) -> None:
    """确保 dev 后端转发容器运行；compose 形态（控制面已容器化）自动跳过。"""
    inspect = await asyncio.create_subprocess_exec(
        docker_bin, "inspect", "agentcraft-control",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await inspect.wait() == 0:
        return  # 控制面已在网络内（compose 形态），DNS 直达

    exists = await asyncio.create_subprocess_exec(
        docker_bin, "inspect", _FORWARDER_NAME,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if await exists.wait() == 0:
        start = await asyncio.create_subprocess_exec(
            docker_bin, "start", _FORWARDER_NAME,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await start.communicate()
        return

    run = await asyncio.create_subprocess_exec(
        docker_bin, "run", "-d",
        "--name", _FORWARDER_NAME,
        "--network", network_name,
        "--network-alias", "agentcraft-control",
        "-e", f"FWD_PORT={target_port}",
        image,
        "node", "-e", _FORWARDER_SCRIPT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await run.communicate()
    if run.returncode != 0:
        logger.warning(
            "启动后端转发容器失败（容器回调 /internal/mcp/call 将不可达）: %s",
            stderr.decode(errors="replace").strip(),
        )
        return
    # bridge 供转发容器访问宿主机（internal 网络本身无 host 路由）
    connect = await asyncio.create_subprocess_exec(
        docker_bin, "network", "connect", "bridge", _FORWARDER_NAME,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await connect.communicate()
    if connect.returncode != 0:
        logger.warning("转发容器 bridge 连接失败: %s", stderr.decode(errors="replace").strip())
    logger.info("后端转发容器已就绪（internal 别名 agentcraft-control → host:%s）", target_port)


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
        "AGENTCRAFT_PROVIDER_MODEL": model,  # 扩展注册 completions provider 用
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
