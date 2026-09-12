"""提交时自动检查（裁决 D9）：skill 全量复用 validate_skill；expert 只跑通用安全规则。

skill revision：validate_skill 原样复用（必填完整性 + 长度 + 四类安全扫描）。
expert revision：无「必填字段」契约（S1 疑点 2），只跑与内容载体无关的安全规则
（API Key / 危险指令 / 可执行代码 / 越狱模板），扫描 persona/methodology 等文本
字段与 task_examples 逐条。正则组直接 import validate_skill 的模块级常量——
规则定义单一来源，不复制正则（仅分派循环重建，因 _scan_field 与 _FIELD_LABELS
耦合 skill 字段名）。
valid = False 当且仅当存在 ERROR 级或越狱模板命中（与 V1 发布闸同语义）。
"""

from harness.mcp.validate_skill import (
    _API_KEY_PATTERNS,
    _DANGEROUS_PATTERNS,
    _EXECUTABLE_CODE_PATTERNS,
    _JAILBREAK_PATTERNS,
    validate_skill,
)

_EXPERT_TEXT_FIELDS = ("name", "description", "persona", "methodology")


def _scan_text(field: str, text: str) -> list[dict]:
    """对单段文本执行四类安全扫描（复用 validate_skill 的正则组）。"""
    issues: list[dict] = []
    for name, pattern in _API_KEY_PATTERNS:
        if pattern.search(text):
            issues.append(
                {
                    "field": field,
                    "rule": "api_key",
                    "level": "ERROR",
                    "message": f"{field} 包含疑似 API Key（{name}）",
                }
            )
    for name, pattern in _DANGEROUS_PATTERNS:
        if pattern.search(text):
            issues.append(
                {
                    "field": field,
                    "rule": "dangerous_command",
                    "level": "ERROR",
                    "message": f"{field} 包含危险指令模式（{name}）",
                }
            )
    for name, pattern in _EXECUTABLE_CODE_PATTERNS:
        if pattern.search(text):
            issues.append(
                {
                    "field": field,
                    "rule": "executable_code",
                    "level": "WARNING",
                    "message": f"{field} 包含可执行代码模式（{name}）",
                }
            )
    for pattern in _JAILBREAK_PATTERNS:
        if pattern.search(text):
            issues.append(
                {
                    "field": field,
                    "rule": "jailbreak_template",
                    "level": "WARNING",
                    "message": f"{field} 命中越狱模板，标记不通过",
                }
            )
    return issues


def run_auto_check(target_type: str, content: dict) -> dict:
    """target_type ∈ {"expert_revision", "skill_revision"}；返回 {valid, issues}。"""
    if target_type == "skill_revision":
        return validate_skill(content)
    issues: list[dict] = []
    for field in _EXPERT_TEXT_FIELDS:
        value = content.get(field)
        if isinstance(value, str) and value:
            issues.extend(_scan_text(field, value))
    for index, example in enumerate(content.get("task_examples") or []):
        if isinstance(example, str) and example:
            issues.extend(_scan_text(f"task_examples[{index}]", example))
    blocking = any(
        issue["level"] == "ERROR" or issue["rule"] == "jailbreak_template" for issue in issues
    )
    return {"valid": not blocking, "issues": issues}
