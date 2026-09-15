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


def test_container_tools_registered_via_callback_phase6(tmp_path):
    """Phase 6（D2 全回调，改写申报——原钉锁 test_container_kind_not_registered_
    in_phase5 为 Phase 5 中间态，D2 用户裁决 2026-09-14 演进为全回调）：
    四 container 工具经 /internal/tools/* callback_path 注册进 task.ts，
    check_code_style 仍注册（harness/container 统一「callback_path 非空即注册」）。"""
    tools = [
        ("read_task_file", "1"),
        ("write_output_file", "1"),
        ("list_task_files", "1"),
        ("query_task_state", "1"),
        ("check_code_style", "1"),
    ]
    src = _gen(tmp_path, tools).read_text(encoding="utf-8")
    for name, callback in (
        ("read_task_file", "/internal/tools/read-task-file"),
        ("write_output_file", "/internal/tools/write-output-file"),
        ("list_task_files", "/internal/tools/list-task-files"),
        ("query_task_state", "/internal/tools/query-task-state"),
    ):
        assert f'"{name}"' in src
        assert callback in src
    assert '"check_code_style"' in src
    assert "/internal/harness/check-code-style" in src


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


# --- Phase 6 T7（D2 全回调）：callback_path/permissions/parameters 对齐 ---


def test_container_descriptors_callback_and_permissions_align_seed():
    """四 container 描述符：callback_path 填参 + permissions 对齐 0002 种子
    tool_catalog.permissions JSONB（服务端强制值源）；check_code_style 的
    permissions 由 None 改为对齐 0002:51 种子（Phase 7 T1 随车项 D11——纯元数据
    对齐，internal.py 回调端点不消费本字段，行为面不变）。"""
    expected_permissions = {
        ("check_code_style", "1"): {"paths": ["/task-files", "/outputs"], "network": False},
        ("read_task_file", "1"): {"paths": ["/task-files"], "network": False},
        ("write_output_file", "1"): {"paths": ["/outputs"], "network": False},
        ("list_task_files", "1"): {"paths": ["/task-files", "/outputs"], "network": False},
        ("query_task_state", "1"): {
            "fields": ["status", "round_summary"],
            "exclude": ["lease_owner", "lease_epoch"],
            "network": False,
        },
    }
    expected_callbacks = {
        ("read_task_file", "1"): "/internal/tools/read-task-file",
        ("write_output_file", "1"): "/internal/tools/write-output-file",
        ("list_task_files", "1"): "/internal/tools/list-task-files",
        ("query_task_state", "1"): "/internal/tools/query-task-state",
        ("check_code_style", "1"): "/internal/harness/check-code-style",
    }
    for key, perms in expected_permissions.items():
        assert PLATFORM_TOOLS[key].permissions == perms
    for key, callback in expected_callbacks.items():
        assert PLATFORM_TOOLS[key].callback_path == callback


def test_write_output_file_parameters_aligned():
    params = PLATFORM_TOOLS[("write_output_file", "1")].parameters
    assert set(params["properties"]) == {"file_name", "content_base64"}
    assert params["required"] == ["file_name", "content_base64"]
    read_params = PLATFORM_TOOLS[("read_task_file", "1")].parameters
    assert set(read_params["properties"]) == {"file_name"}  # 回调体契约 {file_name}


# --- Phase 6 T1（D5 方案 a）：终检前移 + model_input 载荷最后注入 ---
# 数据区豁免：model_input 载荷中的 token 字样原样保留出产物（重排前会被
# 后续 replace 链吞掉/改写/递归展开——①②③ 在重排前为红）。


def test_model_input_token_tools_json_survives(tmp_path):
    # D5 ①：载荷含 __TOOLS_JSON__ 字样 → 原样保留
    src = _gen(
        tmp_path, [("check_code_style", "1")], model_input=("text", "__TOOLS_JSON__")
    ).read_text(encoding="utf-8")
    assert '["text", "__TOOLS_JSON__"]' in src


def test_model_input_token_task_id_survives(tmp_path):
    # D5 ②：载荷含 __TASK_ID__ 字样 → 原样保留
    src = _gen(
        tmp_path, [("check_code_style", "1")], model_input=("text", "__TASK_ID__")
    ).read_text(encoding="utf-8")
    assert '["text", "__TASK_ID__"]' in src


def test_model_input_token_self_referential_survives(tmp_path):
    # D5 ③：载荷自指 __MODEL_INPUT__ → 原样保留（单遍替换不重扫替换文本）
    src = _gen(
        tmp_path, [("check_code_style", "1")], model_input=("text", "__MODEL_INPUT__")
    ).read_text(encoding="utf-8")
    assert '["text", "__MODEL_INPUT__"]' in src


def test_tools_json_payload_token_rejected(tmp_path, monkeypatch):
    # D5 ④：TOOLS JSON 载荷携带 token → 终检仍拒绝写盘（安全化不因重排松动）
    import dataclasses

    import backend.engine.extension_generator as eg

    real = PLATFORM_TOOLS[("check_code_style", "1")]
    poisoned = dataclasses.replace(real, description="x __TOOLS_JSON__ y")
    monkeypatch.setattr(eg, "PLATFORM_TOOLS", {("check_code_style", "1"): poisoned})
    with pytest.raises(ValueError, match="模板占位"):
        _gen(tmp_path, [("check_code_style", "1")])


def test_model_input_lowercase_token_form_preserved(tmp_path):
    # D5 ⑤：合法数据（__x__ 非大写 token 形态）逐字节保留出产物
    src = _gen(tmp_path, [("check_code_style", "1")], model_input=("text", "__x__")).read_text(
        encoding="utf-8"
    )
    assert '["text", "__x__"]' in src
