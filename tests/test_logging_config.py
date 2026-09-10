# tests/test_logging_config.py
import logging

from backend.engine.pi_engine import PiEngine
from backend.logging_config import SensitiveDataFilter, configure_logging


def test_configure_logging_is_idempotent_and_sets_level():
    configure_logging("WARNING")
    configure_logging("WARNING")  # 二次调用不抛 ValueError
    assert logging.getLogger("agentcraft").level == logging.WARNING
    assert logging.getLogger().level == logging.WARNING


def test_filter_redacts_bearer_token():
    rec = logging.LogRecord(
        "agentcraft.t",
        logging.INFO,
        __file__,
        1,
        "Authorization: Bearer abc.def.ghi api_key=sk-123 password=hunter2",
        None,
        None,
    )
    SensitiveDataFilter().filter(rec)
    msg = rec.getMessage()
    assert "abc.def.ghi" not in msg
    assert "sk-123" not in msg
    assert "hunter2" not in msg
    assert "[REDACTED]" in msg


def test_filter_keeps_normal_content():
    rec = logging.LogRecord(
        "agentcraft.t",
        logging.INFO,
        __file__,
        1,
        "task 42 settled in 1200ms",
        None,
        None,
    )
    assert SensitiveDataFilter().filter(rec) is True
    assert rec.getMessage() == "task 42 settled in 1200ms"


def test_no_prompt_content_in_engine_logs():
    # 模拟 pi_engine 对无法解析 stdout 行的处理路径：日志记录不得包含正文
    # （红线 §4.7：记元数据——长度与丢弃计数，不记 prompt/响应正文）
    rec = logging.LogRecord(
        "agentcraft.engine",
        logging.WARNING,
        __file__,
        1,
        "unparsable stdout line (128 bytes, dropped): <body omitted>",
        None,
        None,
    )
    SensitiveDataFilter().filter(rec)
    assert "<body omitted>" not in rec.getMessage() or "128 bytes" in rec.getMessage()


class _NullTransport:
    """PiTransport 最小桩：金丝雀用例只驱动 handle_line 的坏行解析分支。"""

    async def write_line(self, line: str) -> None: ...
    async def readline(self) -> str | None:
        return None

    async def close(self) -> None: ...


async def test_unparsable_stdout_line_never_logs_body(caplog):
    """站点级金丝雀（§4.7 红线）：真实驱动 pi_engine 的坏行分支。

    含金丝雀标记的非 JSON 行经 handle_line 后：金丝雀正文绝不出现在任何
    捕获的日志记录里，长度元数据必须出现。对修复前的 %.120s 正文格式
    该用例必失败（金丝雀会随前 120 字符进入日志），防站点退化为记正文。
    """
    canary = "LEAK-CANARY-prompt-fragment"
    bad_line = f'{canary}: {{"prompt": "秘密内容"}} 不是合法 JSON'
    assert canary in bad_line  # 金丝雀确在输入中（防用例自身退化为永真）

    engine = PiEngine(task_id=7, transport=_NullTransport())
    await engine.handle_line(bad_line)

    messages = [record.getMessage() for record in caplog.records]
    assert all(canary not in message for message in messages), messages
    assert any(str(len(bad_line)) in message for message in messages), messages
    assert engine._unparsable_dropped == 1
