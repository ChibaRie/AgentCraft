"""邮件传输层（task-8-brief）：Protocol + 控制台/生产占位实现。

- ConsoleMailTransport：dev/test 传输，邮件内容整行打日志而非真实投递；action token
  仅当 ``ALLOW_INSECURE_SECRETS=true`` 才可见，否则掩码 ****（日志红线——SensitiveDataFilter
  不识别令牌形态，掩码必须由传输层自律）。
- MailEgressTransport：生产占位（Supplement §9.3 生产闸门）——send 恒 NotImplementedError，
  真实邮件出站（mail-egress）在部署阶段接线，杜绝占位实现静默"假成功"。

选择经 ``Settings.MAIL_TRANSPORT``（console | mailegress；未知值 fail fast，
在 lifespan 启动时即拒绝，不得带病运行）。
"""

from __future__ import annotations

import logging
from typing import Protocol

from backend.config import Settings, get_settings

MAIL_LOGGER_NAME = "agentcraft.mail"
_MAIL_LOG = logging.getLogger(MAIL_LOGGER_NAME)
_TOKEN_MASK = "****"


class MailTransport(Protocol):
    """传输层契约：purpose 定模板；payload 为已解密的白名单 dict（outbox._build_payload）。"""

    async def send(self, *, purpose: str, payload: dict) -> None: ...


class ConsoleMailTransport:
    """控制台传输（dev/test）：purpose/recipient/valid_hours 常规输出；令牌按策略掩码。"""

    async def send(self, *, purpose: str, payload: dict) -> None:
        vars_ = payload.get("vars", {})
        token = str(vars_.get("action_token", ""))
        shown = token if get_settings().ALLOW_INSECURE_SECRETS else _TOKEN_MASK
        _MAIL_LOG.info(
            "mail purpose=%s recipient=%s valid_hours=%s action_token=%s",
            purpose,
            payload.get("recipient"),
            vars_.get("valid_hours"),
            shown,
        )


class MailEgressTransport:
    """生产占位：mail-egress 于部署阶段实现（Supplement §9.3 闸门），恒拒绝投递。"""

    async def send(self, *, purpose: str, payload: dict) -> None:
        raise NotImplementedError("mail-egress 传输在部署阶段接入（Supplement §9.3）")


def transport_from_settings(settings: Settings | None = None) -> MailTransport:
    """按 Settings.MAIL_TRANSPORT 选择传输实现；未知值抛 ValueError（启动即失败）。"""
    resolved = settings or get_settings()
    name = resolved.MAIL_TRANSPORT
    if name == "console":
        return ConsoleMailTransport()
    if name == "mailegress":
        return MailEgressTransport()
    raise ValueError(f"未知 MAIL_TRANSPORT: {name}（仅限 console | mailegress）")
