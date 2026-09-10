"""validate_skill：Skill 内容的格式与安全校验（Engineering Spec §9.1、§7.5）。

纯文本规则校验，不执行任何输入内容，无外部依赖：

1. 必填字段完整性  role/goal/steps/output_requirements/constraints   -> ERROR
2. 内容长度        每个字段不超过 5000 字符                          -> WARNING
3. API Key 检测    sk- / ghp_ / AKIA / xox / AIza 等敏感模式         -> ERROR
4. 危险指令检测    删除文件、格式化磁盘、管道执行脚本等危险模式        -> ERROR
5. 可执行代码检测  import os / subprocess / eval / exec 等           -> WARNING
6. 越狱模板扫描    [INST] / <<< / </system> / 忽略以上所有指令 等      -> 警告并标记不通过

输入：Skill 完整 JSON 对象（dict）。
输出：{"valid": bool, "issues": [{"field", "rule", "level", "message"}]}。
valid = False 当且仅当存在 ERROR 级或越狱模板命中。
"""

import re

REQUIRED_FIELDS = ("role", "goal", "steps", "output_requirements", "constraints")

# 参与内容扫描的字段（含短字段，密钥可能误贴在名称里）
SCAN_FIELDS = (
    "name",
    "description",
    "use_case",
    "role",
    "goal",
    "steps",
    "input_requirements",
    "output_requirements",
    "constraints",
)

MAX_FIELD_LENGTH = 5000

_FIELD_LABELS = {
    "name": "名称",
    "description": "描述",
    "use_case": "使用场景",
    "role": "AI 角色",
    "goal": "任务目标",
    "steps": "工作步骤",
    "input_requirements": "输入要求",
    "output_requirements": "输出要求",
    "constraints": "约束",
}

_API_KEY_PATTERNS = (
    ("openai", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
)

_DANGEROUS_PATTERNS = (
    ("rm-rf", re.compile(r"\brm\s+(-{1,2}[a-zA-Z-]+\s+)*-{1,2}[rf]")),
    ("mkfs", re.compile(r"\bmkfs\b")),
    ("dd", re.compile(r"\bdd\s+if=")),
    ("del-windows", re.compile(r"\b(del\s+/[sqf]|rd\s+/s)\b", re.IGNORECASE)),
    ("format-drive", re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE)),
    ("drop-table", re.compile(r"\bdrop\s+(table|database)\b", re.IGNORECASE)),
    ("pipe-to-shell", re.compile(r"\b(curl|wget)\b[^|;\n]*\|\s*(sudo\s+)?(ba)?sh\b")),
)

_EXECUTABLE_CODE_PATTERNS = (
    ("import", re.compile(r"\bimport\s+(os|subprocess|sys|shutil|socket)\b")),
    ("from-import", re.compile(r"\bfrom\s+(os|subprocess|sys|shutil|socket)\s+import\b")),
    ("eval", re.compile(r"\beval\s*\(")),
    ("exec", re.compile(r"\bexec\s*\(")),
    ("import-hook", re.compile(r"__import__\s*\(")),
    ("os-system", re.compile(r"\bos\.system\b")),
    ("subprocess", re.compile(r"\bsubprocess\.(run|call|Popen|check_output)\b")),
)

# §7.5：高频越狱模板静态扫描；命中即标记不通过
_JAILBREAK_PATTERNS = (
    re.compile(r"\[INST\]"),
    re.compile(r"<<<"),
    re.compile(r"</system>", re.IGNORECASE),
    re.compile(r"忽略(以上|之前|上述|上面的?)(所有)?(的)?(指令|规则|设定|提示)"),
    re.compile(r"现在你是一个"),
)


def _as_text(value):
    return value if isinstance(value, str) else ""


def _field_issues(field, rule, level, message):
    return {"field": field, "rule": rule, "level": level, "message": message}


def _scan_field(field: str, text: str) -> list[dict]:
    """对单个字段执行 3-6 项内容安全扫描。"""
    issues: list[dict] = []
    label = _FIELD_LABELS[field]
    for name, pattern in _API_KEY_PATTERNS:
        if pattern.search(text):
            message = f"{label}包含疑似 API Key（{name}）"
            issues.append(_field_issues(field, "api_key", "ERROR", message))
    for name, pattern in _DANGEROUS_PATTERNS:
        if pattern.search(text):
            message = f"{label}包含危险指令模式（{name}）"
            issues.append(_field_issues(field, "dangerous_command", "ERROR", message))
    for name, pattern in _EXECUTABLE_CODE_PATTERNS:
        if pattern.search(text):
            message = f"{label}包含可执行代码模式（{name}）"
            issues.append(_field_issues(field, "executable_code", "WARNING", message))
    for pattern in _JAILBREAK_PATTERNS:
        if pattern.search(text):
            message = f"{label}命中越狱模板，标记不通过"
            issues.append(_field_issues(field, "jailbreak_template", "WARNING", message))
    return issues


def validate_skill(skill):
    """校验 Skill 内容 dict，返回 {valid, issues}；不修改输入。"""
    issues = []

    # 1. 必填字段完整性（缺键 / 非字符串 / 全空白均视为缺失）
    for field in REQUIRED_FIELDS:
        if not _as_text(skill.get(field)).strip():
            issues.append(
                _field_issues(field, "required", "ERROR", f"{_FIELD_LABELS[field]}不能为空")
            )

    # 2-6. 长度与内容安全扫描（仅对非空字符串字段）
    for field in SCAN_FIELDS:
        text = _as_text(skill.get(field))
        if not text:
            continue
        if len(text) > MAX_FIELD_LENGTH:
            issues.append(
                _field_issues(
                    field,
                    "max_length",
                    "WARNING",
                    f"{_FIELD_LABELS[field]}超过 {MAX_FIELD_LENGTH} 字符",
                )
            )
        issues.extend(_scan_field(field, text))

    blocking = any(
        issue["level"] == "ERROR" or issue["rule"] == "jailbreak_template" for issue in issues
    )
    return {"valid": not blocking, "issues": issues}


def main():
    """命令行入口：读取 JSON 文件参数并打印校验结果（供人工排查使用）。"""
    import json
    import sys

    if len(sys.argv) != 2:
        print("用法: python -m harness.mcp.validate_skill <skill.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as handle:
        result = validate_skill(json.load(handle))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
