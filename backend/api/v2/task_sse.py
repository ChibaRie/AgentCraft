"""V2 任务实时事件流机制（Phase 6 T8b）：D11 帧映射 + 合并排空 + SSE 主循环。

纯机制层（无路由声明）：任务域路由（tasks.py）消费本模块构造 /events 帧与
SSE 流。契约出处：Sup §1.2:27/§1.3、Phase 6 计划 D11
（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）。纪律：

- **D11 帧映射钉死**（``_event_frame``，计划逐字 + 选边见该函数 docstring）：
  task_events.type → SSE 帧；/events 与 SSE 重放共用同一构造（帧形状一致）；
- **流建立序**（Sup §1.3:52）：register（先于重放——注册与重放间隙的事件留在
  订阅缓冲，无补拉间隙）→ DB 重放 sequence>after（与快照同一 owner_session
  事务读出）→ meta 帧（初始 watermark）→ 合并排空（重放与缓冲按 sequence
  去重）→ 实时（队列消费 + 心跳）；断连 finally unsubscribe（幂等静默）；
- 合并去重的提交序依据：sequence 分配经 task 行锁串行化（Sup §1.4:56），
  提交序=序号序——缓冲帧序号恒不小于重放尾，逐序去重即无缺无重；
- 无序号帧（瞬态 delta/tool_event、done、T6b M6 降级帧）不做水位过滤，
  原样透传（容忍义务，tests/test_v2_task_sse.py 钉死）。
"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import HTTPException

from backend.errors import AgentCraftError
from backend.v2 import task_views
from backend.v2.runtime import V2Runtime, owner_session

# 心跳间隔（Sup §1.3:52：每 15s 一条 SSE 注释行；模块常量供测试注入缩短）
_SSE_HEARTBEAT_SECONDS = 15.0


def _event_frame(sequence: int, event_type: str, payload: dict | None) -> dict | None:
    """D11 帧映射（计划逐字）：task_events.type → SSE 帧；/events 与 SSE 重放
    共用本构造（帧形状一致）。映射表：

    - message_saved → ``message_saved`` 帧（**不带正文**：message_id +
      event_sequence + author——正文走 GET /messages 对账，Sup §1.3:50）；
    - status_changed → ``status_changed`` 帧（新 status + abort_reason 可空；
      DB 载荷键 reason 映射为 abort_reason）；
    - round_queued → ``queued`` 帧（**持久**，重放时也发——D11 对 Sup §1.3:40
      瞬态标注的选边，T9 回写登记）；
    - round_settled → ``done``（finish_reason 取载荷）；round_failed →
      ``done(finish_reason=error)``；round_cancelled →
      ``done(finish_reason=aborted)``——失败/取消的 status_changed 帧由同事务
      相邻的 status_changed 事件承载（现行写路径全部成对写入：deadline/reclaim/
      terminator 均先写 status_changed 再写轮事件），本映射**每事件恰一帧、
      id 单调不重复**（同 id 双帧会被按 sequence 幂等应用的客户端去重丢弃
      done 帧，故不做字面双发——T9 回写登记）；
    - round_running → 无独立帧（None；dispatcher 领槽的 status_changed 承载）。
    """
    p = payload or {}
    if event_type == "message_saved":
        raw_seq = p.get("event_sequence")
        return {
            "type": "message_saved",
            "message_id": str(p.get("message_id") or ""),
            "event_sequence": int(raw_seq) if raw_seq is not None else sequence,
            "author": p.get("author"),
        }
    if event_type == "status_changed":
        return {
            "type": "status_changed",
            "status": p.get("status"),
            "abort_reason": p.get("reason"),
        }
    if event_type == "round_queued":
        return {"type": "queued", **p}
    if event_type == "round_settled":
        return {
            "type": "done",
            "finish_reason": p.get("finish_reason") or "stop",
            "usage": p.get("usage") or {},
        }
    if event_type == "round_failed":
        return {"type": "done", "finish_reason": "error", "usage": {}}
    if event_type == "round_cancelled":
        return {"type": "done", "finish_reason": "aborted", "usage": {}}
    return None  # round_running：D11 无独立帧


def _sse_frame(name: str, payload: dict, *, sequence: int | None = None) -> str:
    """SSE 帧文本（V1 帧格式沿用：``event:`` + ``data:`` JSON 行）；持久帧附
    ``id:`` 行（D11）。``data:`` 单行 JSON（ensure_ascii=False）。"""
    lines = []
    if sequence is not None:
        lines.append(f"id: {sequence}")
    lines.append(f"event: {name}")
    lines.append(f"data: {json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def _frame_line(frame: dict, sequence: int | None) -> str:
    """帧 dict → SSE 行：type 提为 event 名，其余键收敛进 data 载荷。"""
    payload = {k: v for k, v in frame.items() if k != "type"}
    return _sse_frame(str(frame.get("type") or "error"), payload, sequence=sequence)


def _drain_queue(queue: "asyncio.Queue[dict]") -> list[dict]:
    """非阻塞排空订阅缓冲（注册→重放窗口内 publish 的帧）。"""
    drained: list[dict] = []
    while True:
        try:
            drained.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return drained


def _merge_stream_frames(
    replay: list[tuple[int, dict]], buffered: list[dict], last_sent: int
) -> tuple[list[tuple[int | None, dict]], int]:
    """合并排空（Sup §1.3:52）：重放帧（升序、每事件恰一帧带序）先行，缓冲帧
    按到达序续接（注册→重放间隙事件必在缓冲，提交序=序号序——task 行锁使
    sequence 分配串行化，缓冲帧序号恒大于重放尾）；持久帧（int event_sequence）
    按 sequence 去重——≤last_sent 丢弃（重放与队列双见同 seq 丢一）；瞬态帧/
    降级帧（无序号）原样保留。返回（合并帧序, 推进后的 last_sent）。"""
    merged: list[tuple[int | None, dict]] = []
    for seq, frame in replay:
        merged.append((seq, frame))
        last_sent = seq
    for frame in buffered:
        seq = frame.get("event_sequence")
        if isinstance(seq, int):
            if seq <= last_sent:
                continue
            last_sent = seq
            merged.append((seq, frame))
        else:
            merged.append((None, frame))
    return merged, last_sent


def _stream_registry(runtime: V2Runtime):
    """实时面注册表解析：executor 缺位（未接线）→ 503——注册到无人 publish 的
    注册表只会产出静默空流，显式失败优于假活。"""
    streams = getattr(getattr(runtime, "executor", None), "streams", None)
    if streams is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "实时事件流未接线"},
        )
    return streams


async def _task_event_stream(
    runtime: V2Runtime, streams, owner_id: str, task_id: str, after: int
) -> AsyncIterator[str]:
    """SSE 主循环（Sup §1.3:52 流建立序，见模块 docstring）：register（先于
    重放）→ DB 重放 sequence>after（升序，同一会话读快照）→ meta 帧（初始
    watermark）→ 合并排空（sequence 去重）→ 实时（队列消费 + 15s 心跳注释行）。
    断连经 finally unsubscribe（幂等静默）。"""
    queue = streams.register(task_id, after=after)
    last_sent = after
    try:
        try:
            async with owner_session(runtime, owner_id) as db:
                data = await task_views.list_task_events(
                    db, owner_id=owner_id, task_id=task_id, after=after, limit=None
                )
        except AgentCraftError as exc:  # 建立后竞态删除/行消失——error 帧收尾
            yield _sse_frame(
                "error",
                {"code": exc.code.value, "message": exc.message, "recoverable": False},
            )
            return
        replay = [
            (seq, frame)
            for seq, event_type, payload_json in data["events"]
            if (frame := _event_frame(seq, event_type, payload_json)) is not None
        ]
        # meta 帧保留（瞬态，无 id 行）：初始 watermark 与流建立时任务快照
        yield _sse_frame(
            "meta",
            {
                "task_id": task_id,
                "status": data["snapshot"]["status"],
                "event_sequence": data["snapshot"]["event_sequence"],
            },
        )
        merged, last_sent = _merge_stream_frames(replay, _drain_queue(queue), last_sent)
        for seq, frame in merged:
            yield _frame_line(frame, seq)
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            seq = frame.get("event_sequence")
            if isinstance(seq, int):
                if seq <= last_sent:  # 实时与重放/缓冲双见——按 sequence 丢一
                    continue
                last_sent = seq
            yield _frame_line(frame, seq if isinstance(seq, int) else None)
    finally:
        streams.unsubscribe(task_id, queue)
