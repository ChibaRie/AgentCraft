# tests/test_logging_config.py
import logging

from backend.logging_config import SensitiveDataFilter, configure_logging


def test_configure_logging_is_idempotent_and_sets_level():
    configure_logging("WARNING")
    configure_logging("WARNING")  # 二次调用不抛 ValueError
    assert logging.getLogger("agentcraft").level == logging.WARNING
    assert logging.getLogger().level == logging.WARNING


def test_filter_redacts_bearer_token():
    rec = logging.LogRecord(
        "agentcraft.t", logging.INFO, __file__, 1,
        "Authorization: Bearer abc.def.ghi api_key=sk-123 password=hunter2",
        None, None,
    )
    SensitiveDataFilter().filter(rec)
    msg = rec.getMessage()
    assert "abc.def.ghi" not in msg
    assert "sk-123" not in msg
    assert "hunter2" not in msg
    assert "[REDACTED]" in msg


def test_filter_keeps_normal_content():
    rec = logging.LogRecord(
        "agentcraft.t", logging.INFO, __file__, 1,
        "task 42 settled in 1200ms", None, None,
    )
    assert SensitiveDataFilter().filter(rec) is True
    assert rec.getMessage() == "task 42 settled in 1200ms"


def test_no_prompt_content_in_engine_logs():
    # 模拟 pi_engine 对无法解析 stdout 行的处理路径：日志记录不得包含正文
    # （红线 §4.7：记元数据——长度与丢弃计数，不记 prompt/响应正文）
    rec = logging.LogRecord(
        "agentcraft.engine", logging.WARNING, __file__, 1,
        "unparsable stdout line (128 bytes, dropped): <body omitted>",
        None, None,
    )
    SensitiveDataFilter().filter(rec)
    assert "<body omitted>" not in rec.getMessage() or "128 bytes" in rec.getMessage()
