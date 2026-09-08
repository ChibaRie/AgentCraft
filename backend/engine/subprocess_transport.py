"""本地子进程 Pi 传输（PI_RUNTIME=subprocess / auto 回退路径）。

以 `node <pi-cli.js> --mode rpc ...` 直跑 Pi（Windows npm shim 是 .cmd 无法被
create_subprocess_exec 执行；直取包内 CLI 入口与容器内 argv 形态一致）。
仅用于无 Docker 环境（Windows 开发机直跑）——它不提供容器沙箱隔离，
生产/交付形态以 docker 运行为准（§7.1 每任务一容器）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from backend.engine.docker_transport import MAX_LINE_BYTES

logger = logging.getLogger("agentcraft")

# Windows npm 全局包默认布局；可用 PI_CLI_JS 覆盖
_PI_NPM_REL = "npm/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
_DEFAULT_CLI = Path(os.environ.get("APPDATA", "")) / _PI_NPM_REL


def resolve_pi_cli_js() -> Path:
    override = os.environ.get("PI_CLI_JS")
    if override:
        return Path(override)
    if _DEFAULT_CLI.exists():
        return _DEFAULT_CLI
    found = shutil.which("pi")
    if found:
        raise RuntimeError(
            f"未找到 pi CLI JS 入口（{_DEFAULT_CLI}）；PI_RUNTIME=subprocess 需要全局安装 "
            f"@earendil-works/pi-coding-agent@0.84.3 或设置 PI_CLI_JS（当前仅发现 shim: {found}）"
        )
    raise RuntimeError(
        "未找到 Pi CLI；请 npm install -g @earendil-works/pi-coding-agent@0.84.3 或设置 PI_CLI_JS"
    )


class SubprocessPiTransport:
    """asyncio 子进程封装：write_line/readline/close（PiTransport 协议）。"""

    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
        self._argv = argv
        self._cwd = Path(cwd)
        self._env = env
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self._cwd.mkdir(parents=True, exist_ok=True)
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self._cwd,
            env=self._env,
            limit=MAX_LINE_BYTES,
        )
        logger.info("Pi 子进程已启动 pid=%s cwd=%s", self._proc.pid, self._cwd)

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
