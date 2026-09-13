"""平台工具常量描述符表（Phase 5 裁决 D3）。

task.ts 注册块所需的 name/label/description/parameters 全部来自本表
（控制面代码自有常量）——revision_tools 行只提供 (tool_id, version) 选择子，
DB 原文永不进入模板（S2 §5 注入面闭环的三层之一）。五工具与 0002 种子
tool_catalog 行一一对应（@version "1"）。

kind 语义：
- "harness"：执行回调为控制面 /internal 端点（callback_path）——当前仅
  check_code_style；Phase 5 生成器按选择子注册。
- "container"：容器内直接实现（文件类工具走挂载卷、query_task_state 走
  Phase 6 回调）——实现与卷模型归 Phase 6（§4.6:98）；Phase 5 生成器对
  命中选择子的 container 工具**不注册**（无实现，注册即运行期报错）。
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
    callback_path: str | None  # kind="harness" 时的 /internal 路径；container 为 None


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
        description="Read a text file from the task's mounted input files (read-only).",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        kind="container",
        callback_path=None,
    ),
    ("write_output_file", "1"): PlatformTool(
        tool_id="write_output_file",
        version="1",
        label="write_output_file",
        description=(
            "Write a text file into the task's outputs directory (collected as deliverables)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        kind="container",
        callback_path=None,
    ),
    ("list_task_files", "1"): PlatformTool(
        tool_id="list_task_files",
        version="1",
        label="list_task_files",
        description="List the task's mounted input files.",
        parameters={"type": "object", "properties": {}, "required": []},
        kind="container",
        callback_path=None,
    ),
    ("query_task_state", "1"): PlatformTool(
        tool_id="query_task_state",
        version="1",
        label="query_task_state",
        description="Query the current task state summary (lease internals excluded).",
        parameters={"type": "object", "properties": {}, "required": []},
        kind="container",
        callback_path=None,
    ),
}
