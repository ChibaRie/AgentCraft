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

Phase 8 T5（§9.10.10 实时保真）：执行器泵之外的系统写点（send/dispatcher/
reclaim/terminator/deadline/sweep/reconcile）经本模块的 ``publish_runtime_frames``
在事务提交后补推帧——帧构造助手（status_frame/queued_frame/done_frame）与运行时
解析（runtime_streams）亦在此单点收口。
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


# ---------------------------------------------------------------------------
# 系统写点 post-commit 推帧（Phase 8 T5，Sup §9.10.10 实时保真义务）
# ---------------------------------------------------------------------------


def runtime_streams(runtime):
    """运行时实时面注册表解析（executor 未接线 → None；鸭子型访问防导入环，
    task_sse._stream_registry 同型语义）。"""
    return getattr(getattr(runtime, "executor", None), "streams", None)


def publish_runtime_frames(runtime, task_id: str, *frames: dict) -> None:
    """系统写点 post-commit 推帧入口（send/dispatcher/reclaim/terminator/deadline/
    sweep/reconcile 各写点）。

    调用点必须在 owner_session 事务提交之后（D16 会话边界外）——事务内推帧会在
    回滚时留下幻影帧（帧已投递而事实未落库）；调用处沿执行器泵先例（task_executor
    事件泵）裸调用、不做 try/except 吞噬（publish 自身有界：空注册表零开销、队列
    满丢弃不阻塞）。executor 未接线 → no-op（实时面缺席不影响落库权威）。
    """
    streams = runtime_streams(runtime)
    if streams is not None:
        for frame in frames:
            streams.publish(str(task_id), frame)


def status_frame(sequence: int, status: str, reason: str | None = None) -> dict:
    """status_changed 实时帧：D11 重放形状（status/abort_reason）+ event_sequence
    水位键——持久帧携带序号才吃合并去重与 ``id:`` 行（注册→重放双见窗口按序去重、
    断线重连游标推进；message_saved 泵先例同语义）。"""
    return {
        "type": "status_changed",
        "status": status,
        "abort_reason": reason,
        "event_sequence": int(sequence),
    }


def queued_frame(sequence: int, round_id: str) -> dict:
    """round_queued → queued 实时帧（D11 持久帧 + 水位键，同 status_frame）。"""
    return {"type": "queued", "round_id": str(round_id), "event_sequence": int(sequence)}


def done_frame(finish_reason: str) -> dict:
    """done 实时帧：瞬态语义与执行器泵 done 同形（无水位键——终态帧不推进游标，
    断线重连经 /events 事实面补拉对账；usage 恒空——失败/取消轮无用量权威）。"""
    return {"type": "done", "finish_reason": finish_reason, "usage": {}}
