"""EventHandler 测试：Pi 事件 → SSE 事件 + 落库的翻译（Engineering Spec §7.3.2/§7.6）。

SSE schema 严格按 §6.6 冻结契约（line 951）：text_delta/thinking_delta/tool_event/
message_saved/done{finish_reason: stop|aborted, usage:{prompt_tokens, completion_tokens}}/error。
落库纪律：assistant 只认 message_end 且 stopReason=stop；aborted/error 半截回复不落库
（§7.6，防止重播种喂回残缺上下文）；user 的 message_start/end 忽略（发送前已落库）。
"""

import json
from pathlib import Path

from backend.engine.event_handler import EventHandler

FRAMES_DIR = Path(__file__).parent / "fixtures" / "pi_frames"


class SpyPersistence:
    def __init__(self) -> None:
        self.assistant_calls: list[dict] = []
        self.tool_calls: list[dict] = []
        self._next_id = 100

    async def persist_assistant(self, content: str, usage: dict) -> dict:
        self._next_id += 1
        call = {"message_id": self._next_id, "content": content, "usage": usage}
        self.assistant_calls.append(call)
        return call

    async def persist_tool(
        self, tool_call_id: str, tool_name: str, content: str, is_error: bool
    ) -> dict:
        self._next_id += 1
        call = {
            "message_id": self._next_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "content": content,
            "is_error": is_error,
        }
        self.tool_calls.append(call)
        return call


def make_handler():
    spy = SpyPersistence()
    return EventHandler(spy.persist_assistant, spy.persist_tool), spy


def frames(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FRAMES_DIR / name).read_text(encoding="utf-8").splitlines()
    ]


async def test_recorded_round_translates_and_persists():
    """回放 faux_basic.jsonl（两轮）：text_delta 按序转发，message_end 落库，settled 出 done。"""
    handler, spy = make_handler()
    sse: list[tuple[str, dict]] = []
    for frame in frames("faux_basic.jsonl"):
        sse.extend(await handler.handle_frame(frame))

    names = [name for name, _ in sse]
    assert "text_delta" in names
    assert names.count("message_saved") == 2, "两轮 prompt 各落库一条"
    assert names.count("done") == 2
    done = sse[-1][1]
    assert done["finish_reason"] == "stop"
    assert set(done["usage"]) == {"prompt_tokens", "completion_tokens"}

    # 流式 delta 拼接 == 落库内容（两轮合并比对）
    streamed = "".join(payload["delta"] for name, payload in sse if name == "text_delta")
    persisted = "".join(call["content"] for call in spy.assistant_calls)
    assert persisted == streamed
    assert streamed.startswith("ECHO:")


async def test_user_message_frames_are_ignored():
    handler, spy = make_handler()
    sse = []
    for frame in [
        {"type": "message_start", "message": {"role": "user", "content": "hi"}},
        {"type": "message_end", "message": {"role": "user", "content": "hi"}},
    ]:
        sse.extend(await handler.handle_frame(frame))
    assert sse == []
    assert spy.assistant_calls == []


async def test_tool_execution_end_persists_tool_message():
    handler, spy = make_handler()
    sse = []
    for frame in [
        {
            "type": "tool_execution_start",
            "toolCallId": "call_1",
            "toolName": "bash",
            "args": {"command": "ls"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "call_1",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": "file-a\nfile-b"}]},
            "isError": False,
        },
        {"type": "agent_settled"},
    ]:
        sse.extend(await handler.handle_frame(frame))

    assert spy.tool_calls == [
        {
            "message_id": 101,
            "tool_call_id": "call_1",
            "tool_name": "bash",
            "content": "file-a\nfile-b",
            "is_error": False,
        }
    ]
    tool_events = [payload for name, payload in sse if name == "tool_event"]
    # 真实帧序（rpc.md）：start 携带 args；end 帧不带 args，结果在 result
    assert tool_events == [
        {"name": "bash", "args": {"command": "ls"}, "status": "start"},
        {"name": "bash", "args": {}, "status": "end", "result": "file-a\nfile-b"},
    ]


async def test_aborted_round_discards_partial_reply():
    """abort 帧序（faux_abort.jsonl）：stopReason=aborted 的 message_end 不落库，
    done.finish_reason=aborted。"""
    handler, spy = make_handler()
    sse: list[tuple[str, dict]] = []
    for frame in frames("faux_abort.jsonl"):
        sse.extend(await handler.handle_frame(frame))

    assert spy.assistant_calls == [], "中止的半截回复不得落库"
    done = [payload for name, payload in sse if name == "done"]
    assert done and done[-1]["finish_reason"] == "aborted"
    assert [payload for name, payload in sse if name == "message_saved"] == []


async def test_tooluse_message_end_is_normal_loop_step():
    """stopReason=toolUse 是 Agent 工具循环的正常中间步（模型请求调用工具）：
    不落库、不发 error 帧，等待工具结果后的后续推理（真实模型 E2E 回归）。"""
    handler, spy = make_handler()
    sse = []
    for frame in [
        {"type": "agent_start"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "toolCall", "id": "c1", "name": "list_directory",
                             "arguments": {"path": "/workspace"}}],
                "stopReason": "toolUse",
                "usage": {},
            },
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "c1", "toolName": "list_directory", "args": {},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "c1", "toolName": "list_directory", "isError": False, "args": {},
            "result": {"content": [{"type": "text", "text": "[DIR] sub"}]},
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "DONE"}],
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        },
        {"type": "agent_settled"},
    ]:
        sse.extend(await handler.handle_frame(frame))

    assert [name for name, _ in sse if name == "error"] == [], "toolUse 不得触发 error 帧"
    assert [call["content"] for call in spy.assistant_calls] == ["DONE"]
    assert [
        (c["tool_call_id"], c["tool_name"], c["content"], c["is_error"])
        for c in spy.tool_calls
    ] == [("c1", "list_directory", "[DIR] sub", False)]


async def test_error_message_not_persisted_and_reports_error():
    handler, spy = make_handler()
    sse = []
    for frame in [
        {"type": "agent_start"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "provider down",
                "usage": {},
            },
        },
        {"type": "agent_settled"},
    ]:
        sse.extend(await handler.handle_frame(frame))

    assert spy.assistant_calls == []
    errors = [payload for name, payload in sse if name == "error"]
    assert errors and errors[0]["recoverable"] is True
    # 红线（§4.7）：Provider errorMessage 属响应正文，不得回传浏览器——
    # SSE error 帧只透出通用话术（canary 仅存于服务端日志）
    assert errors[0]["message"] == "生成回复失败，请重试"
    assert "provider down" not in json.dumps(sse, ensure_ascii=False)
    # 流仍以 done 收尾（前端据此关闭连接）
    assert sse[-1][0] == "done"


async def test_thinking_delta_forwarded():
    handler, _ = make_handler()
    sse = await handler.handle_frame(
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "thinking_delta",
                "contentIndex": 0,
                "delta": "思考中",
            },
        }
    )
    assert sse == [("thinking_delta", {"delta": "思考中"})]


async def test_stream_boundary_events_ignored():
    handler, spy = make_handler()
    sse = []
    for frame in [
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_start", "contentIndex": 0},
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "contentIndex": 0, "content": "x"},
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "toolcall_delta", "contentIndex": 1, "delta": "{}"},
        },
        {"type": "turn_start"},
        {"type": "turn_end", "message": {}, "toolResults": []},
        {"type": "agent_end", "messages": [], "willRetry": False},
        {"type": "queue_update", "steering": [], "followUp": []},
        {"type": "compaction_start", "reason": "threshold"},
        {"type": "compaction_end", "reason": "threshold", "result": {}},
    ]:
        sse.extend(await handler.handle_frame(frame))
    assert sse == []
    assert spy.assistant_calls == []


async def test_multi_block_content_joined_with_newline():
    handler, spy = make_handler()
    await handler.handle_frame(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "第一段"},
                    {"type": "thinking", "thinking": "不应入库"},
                    {"type": "text", "text": "第二段"},
                ],
                "stopReason": "stop",
                "usage": {"input": 10, "output": 5, "totalTokens": 15},
            },
        }
    )
    call = spy.assistant_calls[0]
    assert call["content"] == "第一段\n第二段"
    assert call["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}
