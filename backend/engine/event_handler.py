"""EventHandler：Pi 事件 → SSE 事件 + 落库的翻译器（Engineering Spec §7.3.2/§7.6）。

SSE 事件 schema 严格按 §6.6 冻结契约：
    text_delta{delta} / thinking_delta{delta} / tool_event{name, args, status, result?} /
    message_saved{message_id, content} / done{finish_reason: "stop"|"aborted",
    usage:{prompt_tokens, completion_tokens}} / error{code, message, recoverable}

翻译规则：
- message_update.assistantMessageEvent.type == text_delta / thinking_delta 原样转发；
  text_*/thinking_*/toolcall_* 边界帧 v1 忽略
- tool_execution_start/update/end → tool_event{status}；end 时落库 role=tool 消息
- message_end：role=user 忽略（用户消息发送前已落库）；role=assistant 且
  stopReason=stop 才落库（content 取 text 块按序拼接，usage 映射为
  prompt_tokens/completion_tokens）；stopReason=aborted → 不落库、finish=aborted
  （§7.6 落库纪律：残缺回复不得经重播种喂回上下文）；stopReason=toolUse →
  工具循环正常中间步，静默忽略（真实模型每发起一次工具调用都会经过）；
  stopReason=error → 不落库、发 error 帧，流照常以 done 收尾（§6.6 done
  枚举仅 stop|aborted，错误在流层面与中止同形，前端以 error 帧展示原因）
- agent_settled → done（权威完成信号，释放轮锁由轮处理器执行）
- 其余事件（turn_*/agent_end/queue_update/compaction_* 等）v1 忽略
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger("agentcraft")

PersistAssistant = Callable[[str, dict], Awaitable[dict]]
PersistTool = Callable[[str, str, str, bool], Awaitable[dict]]


def _content_text(content) -> str:
    """AssistantMessage.content 的 text 块按序拼接（thinking/toolCall 不入库）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def _tool_result_text(result) -> str:
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def _map_usage(usage) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    return {
        "prompt_tokens": usage.get("input", 0) or 0,
        "completion_tokens": usage.get("output", 0) or 0,
    }


class EventHandler:
    """消费 Pi 原始事件帧，产出 (sse_name, payload) 序列并触发落库。"""

    def __init__(
        self,
        persist_assistant: PersistAssistant,
        persist_tool: PersistTool,
    ) -> None:
        self._persist_assistant = persist_assistant
        self._persist_tool = persist_tool
        self.finish_reason: str | None = None
        self.last_usage: dict = {}

    async def handle_frame(self, frame: dict) -> list[tuple[str, dict]]:
        """翻译一帧；返回按序应发出的 SSE 事件（可能为空）。"""
        frame_type = frame.get("type")

        if frame_type == "message_update":
            return self._translate_message_update(frame)

        if frame_type in ("tool_execution_start", "tool_execution_update"):
            payload = {
                "name": frame.get("toolName", ""),
                "args": frame.get("args") or {},
                "status": "start" if frame_type == "tool_execution_start" else "update",
            }
            return [("tool_event", payload)]

        if frame_type == "tool_execution_end":
            tool_call_id = frame.get("toolCallId", "")
            tool_name = frame.get("toolName", "")
            is_error = bool(frame.get("isError"))
            result_text = _tool_result_text(frame.get("result"))
            await self._persist_tool(tool_call_id, tool_name, result_text, is_error)
            return [
                (
                    "tool_event",
                    {
                        "name": tool_name,
                        "args": frame.get("args") or {},
                        "status": "end",
                        "result": result_text,
                    },
                )
            ]

        if frame_type == "message_end":
            return await self._translate_message_end(frame)

        if frame_type == "agent_settled":
            usage = self.last_usage or {"prompt_tokens": 0, "completion_tokens": 0}
            return [("done", {"finish_reason": self.finish_reason or "stop", "usage": usage})]

        # agent_start / message_start / turn_* / agent_end / queue_update /
        # compaction_* / auto_retry_* / bash_execution_update：v1 忽略
        return []

    def _translate_message_update(self, frame: dict) -> list[tuple[str, dict]]:
        event = frame.get("assistantMessageEvent") or {}
        event_type = event.get("type")
        if event_type == "text_delta":
            return [("text_delta", {"delta": event.get("delta", "")})]
        if event_type == "thinking_delta":
            return [("thinking_delta", {"delta": event.get("delta", "")})]
        return []

    async def _translate_message_end(self, frame: dict) -> list[tuple[str, dict]]:
        message = frame.get("message") or {}
        role = message.get("role")
        if role == "user":
            return []
        if role != "assistant":
            return []

        stop_reason = message.get("stopReason")
        usage = _map_usage(message.get("usage"))
        content = _content_text(message.get("content"))

        if stop_reason == "stop":
            self.finish_reason = "stop"
            self.last_usage = usage
            saved = await self._persist_assistant(content, usage)
            return [("message_saved", {"message_id": saved["message_id"], "content": content})]

        if stop_reason == "aborted":
            # 中止：丢弃半截回复（§7.6），done 以 aborted 收尾
            self.finish_reason = "aborted"
            return []

        if stop_reason == "toolUse":
            # Agent 工具循环的正常中间步（模型请求调用工具）：不落库、不发
            # error 帧，等待 tool_execution_* 与后续推理；最终回复以 stop 收尾
            return []

        # error 等：不落库；发 error 帧告知可重试，流以 done 收尾
        logger.warning(
            "assistant message_end stopReason=%s（不落库）: %s",
            stop_reason,
            message.get("errorMessage", ""),
        )
        return [
            (
                "error",
                {
                    "code": "ENGINE_ERROR",
                    "message": message.get("errorMessage") or "生成回复失败，请重试",
                    "recoverable": True,
                },
            )
        ]
