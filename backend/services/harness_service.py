"""Harness 内部工具业务逻辑（手册 §6.8 check-code-style、§9.2）。

- 路径解析：容器内 `/workspace` 相对路径 → 任务 workdir 主机目录；拒绝
  绝对路径、`..` 与解析后逃逸（符号链接跟随后再校验 containment）
- ruff 执行：`ruff format --check` + `ruff check`（30s 超时）；rc<=1 属
  正常结果（1=有问题），rc>=2 或超时 → 502
- 内部认证：/internal/harness/check-code-style 复用 X-Task-Token 三重校验
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from backend.services.user_service import UserSystemError

logger = logging.getLogger("agentcraft")


class HarnessPathInvalidError(UserSystemError):
    status_code = 400
    code = "INVALID_PATH"


class RuffExecutionError(UserSystemError):
    status_code = 502
    code = "RUFF_EXECUTION_FAILED"


async def resolve_workspace_path(workdir_root: Path, relative: str | None) -> Path:
    """把容器内相对路径解析回主机目录，越界/非法一律拒绝（§7.9 路径校验）。"""
    root = Path(workdir_root).resolve()
    if not relative or not relative.strip():
        return root
    candidate_posix = PurePosixPath(relative.strip())
    if candidate_posix.is_absolute() or relative.strip().startswith("/"):
        raise HarnessPathInvalidError("path 必须是 /workspace 内的相对路径")
    if any(part == ".." for part in candidate_posix.parts):
        raise HarnessPathInvalidError("path 不允许包含 ..")
    candidate = (root / Path(*candidate_posix.parts)).resolve()
    if candidate != root and root not in candidate.parents:
        raise HarnessPathInvalidError("path 越出工作区边界")
    if not candidate.exists():
        raise HarnessPathInvalidError("path 不存在")
    return candidate


def _parse_lint_line(line: str) -> dict | None:
    """`file:line:col: CODE message` → issue；不匹配返回 None。"""
    parts = line.split(":", 3)
    if len(parts) < 4:
        return None
    try:
        line_no = int(parts[1])
    except ValueError:
        return None
    message = parts[3].strip()
    code, _, rest = message.partition(" ")
    return {
        "file": parts[0].replace("\\", "/"),
        "line": line_no,
        "code": code or "lint",
        "message": rest.strip() or message,
    }


def _parse_format_line(line: str) -> dict | None:
    """`would reformat: file` → issue（format 无行号/代码）。"""
    text = line.strip()
    marker = "would reformat:"
    if not text.startswith(marker):
        return None
    return {
        "file": text[len(marker) :].strip().replace("\\", "/"),
        "line": None,
        "code": "format",
        "message": "would reformat",
    }


def _collect_issues(fmt_out: str, lint_out: str) -> list[dict]:
    """解析两段 ruff 输出为 issue 列表。"""
    results: list[dict] = []
    for line in fmt_out.splitlines():
        issue = _parse_format_line(line)
        if issue:
            results.append(issue)
    for line in lint_out.splitlines():
        issue = _parse_lint_line(line)
        if issue:
            results.append(issue)
    return results


async def run_ruff_checks(
    directory: Path, *, runner=None, timeout: float = 30.0
) -> dict:
    """对目录（或单文件）执行 ruff format --check + ruff check，汇总 issues（§6.8）。"""
    if runner is None:
        runner = _default_runner

    if directory.is_dir():
        cwd, targets = directory, ["."]
    else:
        cwd, targets = directory.parent, [directory.as_posix()]

    try:
        fmt_rc, fmt_out, fmt_err = await asyncio.wait_for(
            runner(["ruff", "format", "--check", *targets], cwd), timeout
        )
        lint_rc, lint_out, lint_err = await asyncio.wait_for(
            runner(["ruff", "check", "--output-format=concise", *targets], cwd), timeout
        )
    except asyncio.TimeoutError as exc:
        logger.warning("ruff 超时（%s）", directory)
        raise RuffExecutionError("ruff 执行超时") from exc
    except Exception as exc:
        # 环境类失败（ruff 缺失/子进程不可用）统一 502；细节入日志
        logger.warning("ruff 执行环境失败（%s）: %s", type(exc).__name__, exc)
        raise RuffExecutionError("ruff 不可执行") from exc

    if fmt_rc >= 2:
        logger.warning("ruff format --check 执行失败 rc=%s: %s", fmt_rc, fmt_err[:200])
        raise RuffExecutionError("ruff format 执行失败")
    if lint_rc >= 2:
        logger.warning("ruff check 执行失败 rc=%s: %s", lint_rc, lint_err[:200])
        raise RuffExecutionError("ruff check 执行失败")

    results = _collect_issues(fmt_out, lint_out)
    return {"passed": not results, "issues": results,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def _resolve_ruff() -> str:
    """优先 PATH，其次当前解释器同目录（venv 部署形态）。"""
    import os
    import shutil
    import sys

    found = shutil.which("ruff")
    if found:
        return found
    exe = "ruff.exe" if os.name == "nt" else "ruff"
    candidate = Path(sys.executable).parent / exe
    return str(candidate) if candidate.exists() else "ruff"


async def _default_runner(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    argv = [_resolve_ruff() if argv and argv[0] == "ruff" else argv[0], *argv[1:]]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else 2,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )
