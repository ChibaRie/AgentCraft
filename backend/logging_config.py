# backend/logging_config.py
"""集中日志配置：结构化格式 + 敏感值脱敏。运行时唯一入口，禁止散落 basicConfig。"""
import logging
import logging.config
import re

_REDACTIONS = [
    (re.compile(r"(?i)authorization:\s*bearer\s+\S+"), "Authorization: Bearer [REDACTED]"),
    (re.compile(r"(?i)\bapi_key[=:]\s*\S+"), "api_key=[REDACTED]"),
    (re.compile(r"(?i)\bpassword[=:]\s*\S+"), "password=[REDACTED]"),
    (re.compile(r"(?i)\btotp\b\s*[:=]\s*\S+"), "totp=[REDACTED]"),
]


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pattern, repl in _REDACTIONS:
            msg = pattern.sub(repl, msg)
        record.msg = msg
        record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {"sensitive": {"()": SensitiveDataFilter}},
            "formatters": {
                "std": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "filters": ["sensitive"],
                    "formatter": "std",
                }
            },
            "root": {"level": level.upper(), "handlers": ["console"]},
            # 必须显式设置 agentcraft logger 的 level（子 logger 默认 NOTSET，
            # .level 断言与生效级别都依赖此段；propagate 保持 True 以兼容 caplog）
            "loggers": {"agentcraft": {"level": level.upper(), "propagate": True}},
        }
    )
