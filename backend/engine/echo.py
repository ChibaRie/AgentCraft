"""EchoEngine：阶段 4 Mock 引擎，用最简行为冻结 SSE 事件契约（§6.6）。

真实 Pi 引擎阶段将替换为 PiEngineManager 派发的进程引擎；API 层只依赖
EngineEvent 事件流，因此替换引擎时前端与 SSE 契约不用动。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class EngineEvent:
    """引擎向 API 层输出的中间事件；final 表示一轮回复完成并附 usage。"""

    name: str  # "text_delta" | "thinking_delta" | "tool_event" | "final"
    payload: dict


_TEXT_CHUNK_SIZE = 3


class EchoEngine:
    """原样回显用户输入：text_delta 分帧流出，最后以 final 交付完整回复。"""

    def __init__(self, chunk_size: int = _TEXT_CHUNK_SIZE) -> None:
        self._chunk_size = max(1, chunk_size)

    async def stream(self, content: str) -> AsyncIterator[EngineEvent]:
        for start in range(0, len(content), self._chunk_size):
            yield EngineEvent(
                "text_delta", {"delta": content[start : start + self._chunk_size]}
            )
        yield EngineEvent(
            "final",
            {
                "content": content,
                "usage": {"prompt_tokens": len(content), "completion_tokens": len(content)},
            },
        )
