"""传输层大行还原回归测试（§7.6 JSONL 帧协议）。

背景（2026-09-08 任务 4 崩溃）：Agent read 855KB 图片后，Pi 将含 base64
图像块的 tool_result 以单行 JSONL（约 1.17MB）写向 stdout；
``DockerCliTransport`` 创建子进程时未设 ``limit``，asyncio StreamReader
默认 64KB 行上限在传输层抛 ``ValueError``，行未及到达引擎的
``MAX_LINE_BYTES`` 防护（pi_engine.py）即崩溃；崩溃恢复重建容器后
Agent 重新读图，同因 3 次耗尽，任务标记 failed。

契约：``DockerCliTransport.readline()`` 必须完整还原引擎层
``MAX_LINE_BYTES`` 以内的任意单行，不得在传输层提前截断或抛错。
用桩进程（fake docker CLI）替代真实 docker run 隔离测试读取路径。
"""

import asyncio
import sys
from pathlib import Path

from backend.engine.docker_transport import DockerCliTransport, ContainerSpec
from backend.engine.pi_engine import MAX_LINE_BYTES


def _make_spec(tmp_path: Path) -> ContainerSpec:
    return ContainerSpec(
        container_name=f"test-pi-{tmp_path.name}",
        image="fake-image:test",
        argv=["pi"],
        env={},
        mounts=[],
        network_name="agentcraft-internal",
        labels={},
    )


def _make_stub(tmp_path: Path, line_chars: int) -> str:
    """生成忽略 docker 参数的桩 CLI：输出单行 line_chars 字符后驻留。"""
    emit = tmp_path / f"emit_{line_chars}.py"
    emit.write_text(
        "import sys, time\n"
        f"sys.stdout.write('X' * {line_chars} + chr(10))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    stub = tmp_path / f"fake-docker-{line_chars}.cmd"
    stub.write_text(
        f'@echo off\r\n"{sys.executable}" "{emit}"\r\n', encoding="ascii"
    )
    return str(stub)


async def _read_big_line(tmp_path: Path, line_chars: int) -> str:
    transport = DockerCliTransport(_make_spec(tmp_path), docker_bin=_make_stub(tmp_path, line_chars))
    try:
        await transport.start()
        line = await asyncio.wait_for(transport.readline(), timeout=15)
        assert line is not None, "stdout 提前 EOF：桩进程未输出即退出"
        return line
    finally:
        await transport.close()


async def test_readline_delivers_line_over_asyncio_default_limit(tmp_path):
    """超过 asyncio 默认 64KB 行上限的单行必须完整送达（回归：任务 4 崩溃）。"""
    line = await _read_big_line(tmp_path, 70_000)
    assert len(line) == 70_000


async def test_readline_delivers_line_up_to_engine_max_line_bytes(tmp_path):
    """引擎 MAX_LINE_BYTES 以内的大行（真实图像帧量级）必须在传输层完整还原。"""
    line = await _read_big_line(tmp_path, MAX_LINE_BYTES - 100_000)
    assert len(line) == MAX_LINE_BYTES - 100_000
