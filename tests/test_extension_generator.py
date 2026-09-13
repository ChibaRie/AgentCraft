"""扩展生成器 Phase 5 改版测试（裁决 D3/D4：常量表 × 选择子 + 模板安全化）。"""

import re

import pytest

from backend.engine.extension_generator import ExtensionGenerator
from backend.engine.platform_tools import PLATFORM_TOOLS


def _gen(tmp_path, tools, provider="openai", **kw):
    return ExtensionGenerator(tmp_path).generate(1, tools, provider, **kw)


def test_harness_tool_registered_via_constant_table(tmp_path):
    path = _gen(tmp_path, [("check_code_style", "1")])
    src = path.read_text(encoding="utf-8")
    assert '"check_code_style"' in src
    assert "/internal/harness/check-code-style" in src
    assert "server_id" not in src  # V1 MCP 语义消亡
    assert "/internal/mcp/call" not in src  # 回调端点已删（T4 契约）
    assert "pi.registerProvider" in src  # provider 块保留


def test_unknown_tool_rejected(tmp_path):
    with pytest.raises(ValueError, match="未知平台工具"):
        _gen(tmp_path, [("no_such_tool", "1")])


def test_container_kind_not_registered_in_phase5(tmp_path):
    path = _gen(tmp_path, [("read_task_file", "1"), ("check_code_style", "1")])
    src = path.read_text(encoding="utf-8")
    assert "read_task_file" not in src  # kind=container 不注册（Phase 6 随卷模型）
    assert '"check_code_style"' in src


def test_no_template_token_survives(tmp_path):
    path = _gen(tmp_path, [("check_code_style", "1")], provider="faux")
    src = path.read_text(encoding="utf-8")
    assert re.search(r"__[A-Z_]+__", src) is None  # 全部占位符已替换且数据不含 token


def test_generated_ts_shape(tmp_path):
    # 形态断言（对抗转义类回归）：首行必须是 import（模板首行续行反斜杠一旦
    # 写成双反斜杠，产物首行会是孤立 `\`，本用例即红）
    src = _gen(tmp_path, [("check_code_style", "1")]).read_text(encoding="utf-8")
    assert src.splitlines()[0].startswith("import ")
    assert "\\" not in src.splitlines()[0]


def test_model_input_param_wiring(tmp_path):
    src = _gen(tmp_path, [("check_code_style", "1")]).read_text(encoding="utf-8")
    assert '["text", "image"]' in src  # 默认保持 V1 行为
    src2 = _gen(tmp_path, [("check_code_style", "1")], model_input=("text",)).read_text(
        encoding="utf-8"
    )
    assert '["text"]' in src2


def test_v1_transition_selector_shape(tmp_path):
    from backend.engine.pi_engine_manager import _V1_TRANSITION_TOOLS

    assert _V1_TRANSITION_TOOLS == (("check_code_style", "1"),)
    assert _V1_TRANSITION_TOOLS[0] in PLATFORM_TOOLS
