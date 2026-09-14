"""任务实时帧注册表（Phase 6 T6a）——SSE 实时面与事实面的解耦层。

executor 事件泵把每一条翻译帧（瞬态流式增量 + 事实帧 + done）publish 到本注册
表；SSE 路由（Phase 6 T8a）按 task_id register 领取 asyncio.Queue 消费。纪律：

- **空注册表零开销 no-op**：publish 在无订阅者时 dict 未命中即返回（事件泵
  主路径不因实时面缺席产生任何额外分配）；
- 队列有界（与 V1 manager 轮队列同型防线）：text_delta 洪泛时超限丢弃，
  不阻塞事件泵（事实帧落库不依赖实时面投递成功——落库序列才是权威）；
- ``after`` 水位：订阅方可声明「只收 sequence > after 的帧」——事实帧携带
  event_sequence 时按水位过滤（断线重连补拉走 /events 事实面，实时面只
  承接增量；T8a 消费本语义）。

帧形态：``{"type": <name>, **payload}``（message_saved 事实帧额外携带
event_sequence/author；done/瞬态帧无 sequence，不做水位过滤）。
"""

from __future__ import annotations

import asyncio
import logging
import weakref

logger = logging.getLogger("agentcraft.task.streams")

# 单订阅队列上限：流式增量洪泛的内存防线（与 pi_engine_manager._ROUND_QUEUE_MAX 同量级）
_QUEUE_MAX = 2000

# 实例登记（WeakSet）：conftest 清理夹具消费（T6a M-4 交接）——仅测试残留实例的
# 同步引用清场；WeakSet 不阻止 GC，生产生命周期不受影响
LIVE_REGISTRIES: "weakref.WeakSet[TaskStreamRegistry]" = weakref.WeakSet()


class _Subscription:
    """单个订阅者：消费队列 + 接入水位。"""

    __slots__ = ("queue", "after")

    def __init__(self, after: int) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self.after = after


class TaskStreamRegistry:
    """task_id → 订阅者集合的进程内注册表（executor 与 SSE 路由共享）。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[_Subscription]] = {}
        LIVE_REGISTRIES.add(self)

    def register(self, task_id: str, after: int = 0) -> asyncio.Queue:
        """注册订阅者并返回消费队列；``after`` 水位之前的带序帧不投递。"""
        sub = _Subscription(int(after))
        self._subs.setdefault(str(task_id), []).append(sub)
        return sub.queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        """注销订阅者（队列由调用方排空/丢弃；缺失即幂等静默）。"""
        subs = self._subs.get(str(task_id))
        if not subs:
            return
        self._subs[str(task_id)] = [s for s in subs if s.queue is not queue]
        if not self._subs[str(task_id)]:
            self._subs.pop(str(task_id), None)

    def publish(self, task_id: str, frame: dict) -> None:
        """向 task 的全部订阅者投递一帧（空注册表零开销 no-op）。

        队列满 → 丢弃该帧（流式增量可容忍；事实帧权威在 task_events 落库面）。
        水位过滤：仅对携带 event_sequence 的帧生效（瞬态帧/done 无序号直投）。
        """
        subs = self._subs.get(str(task_id))
        if not subs:
            return
        seq = frame.get("event_sequence")
        for sub in list(subs):
            if seq is not None and seq <= sub.after:
                continue
            try:
                sub.queue.put_nowait(frame)
            except asyncio.QueueFull:
                logger.warning(
                    "实时帧队列已满，丢弃帧（task_id=%s type=%s）",
                    task_id,
                    frame.get("type"),
                )
