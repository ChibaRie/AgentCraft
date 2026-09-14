"""平台工具描述符表（Phase 5 裁决 D3；Phase 6 D2 全回调改版）。

task.ts 注册块所需的 name/label/description/parameters 全部来自本表
（控制面代码自有常量）——revision_tools 行只提供 (tool_id, version) 选择子，
DB 原文永不进入模板（S2 §5 注入面闭环的三层之一）。五工具与 0002 种子
tool_catalog 行一一对应（@version "1"）。

kind 语义（D2 全回调，2026-09-14 用户裁决）：
- "harness"：执行回调为控制面 /internal 端点（callback_path）——当前仅
  check_code_style；
- "container"：Phase 5 曾为「容器内直接实现（不注册）」；Phase 6 起四工具
  全部走 /internal/tools/* 控制面回调（容器零数据挂载），callback_path 填参
  后由生成器按「callback_path 非空即注册」统一注册。permissions 为服务端
  强制声明（paths/exclude/network，值对齐 0002 种子 tool_catalog.permissions
  JSONB），供回调端点读取校验请求形态——不进 task.ts 模板（DB 原文永不
  进入模板的红线不变，本表仍为控制面自有常量）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformTool:
    tool_id: str
    version: str
    label: str
    description: str
    parameters: dict  # JSON schema（task.ts registerTool 的 parameters）
    kind: str  # "harness" | "container"
    callback_path: str | None  # /internal 回调路径；D2 后五工具均非空
    # 服务端权限声明（D2：值对齐 0002 种子 tool_catalog.permissions JSONB）；
    # 供 /internal/tools 回调端点强制（paths/exclude/network），不进模板
    permissions: dict | None = None


PLATFORM_TOOLS: dict[tuple[str, str], PlatformTool] = {
    ("check_code_style", "1"): PlatformTool(
        tool_id="check_code_style",
        version="1",
        label="check_code_style",
        description="Run ruff format --check and ruff check on a path inside /workspace",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": [],
        },
        kind="harness",
        callback_path="/internal/harness/check-code-style",
    ),
    ("read_task_file", "1"): PlatformTool(
        tool_id="read_task_file",
        version="1",
        label="read_task_file",
        description="Read a task input file by name via the control-plane callback.",
        parameters={
            "type": "object",
            "properties": {"file_name": {"type": "string"}},
            "required": ["file_name"],
        },
        kind="container",
        callback_path="/internal/tools/read-task-file",
        permissions={"paths": ["/task-files"], "network": False},
    ),
    ("write_output_file", "1"): PlatformTool(
        tool_id="write_output_file",
        version="1",
        label="write_output_file",
        description=(
            "Write a file as a task output artifact via the control-plane callback "
            "(collected as deliverables)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_name": {"type": "string"},
                "content_base64": {"type": "string"},
            },
            "required": ["file_name", "content_base64"],
        },
        kind="container",
        callback_path="/internal/tools/write-output-file",
        permissions={"paths": ["/outputs"], "network": False},
    ),
    ("list_task_files", "1"): PlatformTool(
        tool_id="list_task_files",
        version="1",
        label="list_task_files",
        description="List the task's input files (name/sha256/size metadata).",
        parameters={"type": "object", "properties": {}, "required": []},
        kind="container",
        callback_path="/internal/tools/list-task-files",
        permissions={"paths": ["/task-files", "/outputs"], "network": False},
    ),
    ("query_task_state", "1"): PlatformTool(
        tool_id="query_task_state",
        version="1",
        label="query_task_state",
        description="Query the current task state summary (lease internals excluded).",
        parameters={"type": "object", "properties": {}, "required": []},
        kind="container",
        callback_path="/internal/tools/query-task-state",
        permissions={
            "fields": ["status", "round_summary"],
            "exclude": ["lease_owner", "lease_epoch"],
            "network": False,
        },
    ),
}
