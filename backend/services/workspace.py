"""工作目录解析（Engineering Spec §6.6 GET /api/workspaces + POST /api/tasks）。

API 只接受授权根目录内的相对路径；服务层负责归一化、越界/符号链接逃逸校验，
并派生存储值 /workspaces/authorized[/<relative-path>]。绝不接收主机绝对路径。
"""

import re
from pathlib import Path

from backend.services.user_service import UserSystemError


class WorkdirInvalidError(UserSystemError):
    status_code = 400
    code = "WORKDIR_INVALID"


class WorkspaceNotFoundError(UserSystemError):
    status_code = 404
    code = "WORKDIR_NOT_FOUND"


# 控制面内的规范授权根（DB CHECK ck_tasks_workdir 与初始迁移均硬编码此前缀）
CANONICAL_AGENT_ROOT = "/workspaces/authorized"

_SPLIT_PATTERN = re.compile(r"[\\/]+")
# Windows 文件名语义：结尾点/空格会被 Win32 剥除产生路径别名，一并拒绝
_TRIM_CHARS = " . \t"


# tasks.workdir 为 VARCHAR(500)（DB 设计 §3.5）：相对段长度 = 500 - 根前缀 - 分隔符
_MAX_RELATIVE_LENGTH = 500 - len(CANONICAL_AGENT_ROOT) - 1


def parse_relative_workdir(raw: str | None) -> str:
    """归一化用户输入的相对路径；根目录返回空串，非法输入抛 400。"""
    if raw is None:
        return ""
    value = raw.strip()
    if value == "":
        return ""
    if (
        value.startswith(("/", "\\"))
        or value.startswith("\\\\")
        or (len(value) >= 2 and value[1] == ":")
    ):
        raise WorkdirInvalidError("工作目录必须为授权根目录内的相对路径")
    parts = [part for part in _SPLIT_PATTERN.split(value) if part not in ("", ".")]
    if not parts:
        return ""
    for part in parts:
        if part == "..":
            raise WorkdirInvalidError("工作目录不允许路径穿越（..）")
        if part != part.rstrip(_TRIM_CHARS) or part != part.lstrip(_TRIM_CHARS):
            raise WorkdirInvalidError("工作目录的路径段不能以点或空格开头/结尾")
        if any(ord(char) < 32 or ord(char) == 127 for char in part):
            raise WorkdirInvalidError("工作目录包含非法字符")
    relative = "/".join(parts)
    if len(relative) > _MAX_RELATIVE_LENGTH:
        raise WorkdirInvalidError("工作目录路径过长")
    return relative


def resolve_workspace_dir(root: Path, relative: str) -> Path:
    """将相对路径解析到授权根目录内；拒绝符号链接逃逸与越界。

    no-follow 语义：从根到目标的每一级组件都不得是符号链接。
    """
    root_real = Path(root).resolve()
    if not relative:
        return root_real
    current = root_real
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise WorkdirInvalidError("工作目录路径链包含符号链接")
    resolved = current.resolve()
    if resolved != root_real and root_real not in resolved.parents:
        raise WorkdirInvalidError("工作目录越出授权根目录")
    return resolved


def derive_stored_workdir(relative: str) -> str:
    """派生存储值：基于规范常量（与 tasks.workdir 的 CHECK 约束绑定）。"""
    if not relative:
        return CANONICAL_AGENT_ROOT
    return f"{CANONICAL_AGENT_ROOT}/{relative}"
