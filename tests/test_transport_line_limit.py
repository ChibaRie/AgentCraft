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
from types import SimpleNamespace

from backend.engine.docker_transport import MAX_LINE_BYTES, ContainerSpec, DockerCliTransport


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
    transport = DockerCliTransport(
        _make_spec(tmp_path), docker_bin=_make_stub(tmp_path, line_chars)
    )
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


async def test_readline_drops_oversize_line_and_keeps_transport_alive(tmp_path):
    """超过 MAX_LINE_BYTES 的行被丢弃为空行哨兵，传输保持存活，后续行正常送达。"""
    emit = tmp_path / "emit_oversize.py"
    emit.write_text(
        "import sys, time\n"
        f"sys.stdout.write('X' * ({MAX_LINE_BYTES} + 1_000_000) + chr(10))\n"
        "sys.stdout.write('OK' + chr(10))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    stub = tmp_path / "fake-docker-oversize.cmd"
    stub.write_text(f'@echo off\r\n"{sys.executable}" "{emit}"\r\n', encoding="ascii")
    transport = DockerCliTransport(_make_spec(tmp_path), docker_bin=str(stub))
    try:
        await transport.start()
        first = await asyncio.wait_for(transport.readline(), timeout=30)
        assert first == "", (
            f"超限行应返回空行哨兵，实际 {type(first)} len={len(first) if first else 0}"
        )
        second = await asyncio.wait_for(transport.readline(), timeout=15)
        assert second == "OK"
    finally:
        await transport.close()


async def test_subprocess_transport_delivers_line_over_default_limit(tmp_path):
    """无 Docker 回退通道同样必须还原超默认上限的大行。"""
    from backend.engine.subprocess_transport import SubprocessPiTransport

    emit = tmp_path / "emit_sub.py"
    emit.write_text(
        "import sys, time\n"
        "sys.stdout.write('X' * 70_000 + chr(10))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    transport = SubprocessPiTransport([sys.executable, str(emit)], cwd=tmp_path)
    try:
        await transport.start()
        line = await asyncio.wait_for(transport.readline(), timeout=15)
        assert line is not None, "stdout 提前 EOF：子进程未输出即退出"
        assert len(line) == 70_000
    finally:
        await transport.close()


async def test_subprocess_readline_drops_oversize_line_and_keeps_transport_alive(tmp_path):
    """无 Docker 回退通道同样丢弃超限帧为空行哨兵，传输保持存活，后续行正常送达。"""
    from backend.engine.subprocess_transport import SubprocessPiTransport

    emit = tmp_path / "emit_sub_oversize.py"
    emit.write_text(
        "import sys, time\n"
        f"sys.stdout.write('X' * ({MAX_LINE_BYTES} + 1_000_000) + chr(10))\n"
        "sys.stdout.write('OK' + chr(10))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    transport = SubprocessPiTransport([sys.executable, str(emit)], cwd=tmp_path)
    try:
        await transport.start()
        first = await asyncio.wait_for(transport.readline(), timeout=30)
        assert first == "", (
            f"超限行应返回空行哨兵，实际 {type(first)} len={len(first) if first else 0}"
        )
        second = await asyncio.wait_for(transport.readline(), timeout=15)
        assert second == "OK"
    finally:
        await transport.close()


class _FakeMuxStream:
    """按脚本回放 demux chunk 的假 attach 流。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    async def read_out(self):
        if self._chunks:
            return SimpleNamespace(data=self._chunks.pop(0))
        return None


async def _assembled_lines(chunks: list[bytes], count: int) -> list[str]:
    from backend.engine.docker_transport import _LineAssembler

    assembler = _LineAssembler(_FakeMuxStream(chunks).read_out)
    return [await assembler.readline() for _ in range(count)]


async def test_line_assembler_joins_chunk_split_line():
    """一行跨 3 个 chunk 必须拼装还原（Docker demux 按 write 边界分块）。"""
    lines = await _assembled_lines([b"X" * 40_000, b"X" * 40_000, b"X\n"], 1)
    assert lines == ["X" * 80_001]


async def test_line_assembler_splits_coalesced_chunk():
    """多个 write 合入同一 chunk 时必须逐行返回。"""
    lines = await _assembled_lines([b'{"a":1}\n{"b":2}\n{"c":3}\n'], 3)
    assert lines == ['{"a":1}', '{"b":2}', '{"c":3}']


async def test_line_assembler_handles_crlf_and_partial_tail():
    """\\r\\n 行尾正常剥离；EOF 残留无换行尾巴一次性返回不吞数据。"""
    lines = await _assembled_lines([b'{"a":1}\r\n{"b":', b"2", b"}"], 2)
    assert lines == ['{"a":1}', '{"b":2}']


async def test_line_assembler_clean_eof_returns_none():
    lines = await _assembled_lines([], 1)
    assert lines == [None]


async def test_line_assembler_drops_oversize_line_and_keeps_following():
    """超 MAX_LINE_BYTES 的行在组装层被丢弃为空行哨兵，后续行正常送达。"""
    lines = await _assembled_lines(
        [b"X" * (MAX_LINE_BYTES + 100), b"\n", b"OK\n", b"END\n"], 3
    )
    assert lines == ["", "OK", "END"]


async def test_line_assembler_hard_ceiling_without_separator():
    """分隔符迟迟不来的异常流越过 2×MAX_LINE_BYTES 硬顶后清空返回哨兵。"""
    chunks = [b"X" * (8 * 1024 * 1024) for _ in range(9)]  # 72MiB 无换行
    lines = await _assembled_lines(chunks, 1)
    assert lines == [""]


async def test_line_assembler_drops_single_chunk_oversize_line():
    """已含分隔符的整块超限行同样丢弃为空行哨兵（防御性，对齐 CLI 语义）。"""
    lines = await _assembled_lines(
        [b"X" * (MAX_LINE_BYTES + 100) + b"\n", b"OK\n"], 2
    )
    assert lines == ["", "OK"]
