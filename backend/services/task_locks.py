"""任务级进程内互斥（Engineering Spec §6.6 并发约束、§7.2.1 的阶段 4 替代）。

- data lock：串行化「上传提交（配额核对 + TaskFile 落库 + manifest 冻结复核）」与
  「首条消息状态流转」，关闭配额 TOCTOU 与任务开始后文件仍可入库的窗口。
- round lock：一轮回复从发送到流结束持有；被持有时发送返回 429 + Retry-After（§6.1）。

Pi 引擎阶段将替换为 §7.2.1 的跨进程 task mutation lock，接口保持一致。
单进程 uvicorn 下 asyncio.Lock 语义成立；多进程部署是 Pi 阶段的迁移动因之一。
"""

import asyncio

_data_locks: dict[int, asyncio.Lock] = {}
_round_locks: dict[int, asyncio.Lock] = {}


def task_data_lock(task_id: int) -> asyncio.Lock:
    """上传提交 / 首条消息流转共用的短临界区锁。"""
    lock = _data_locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _data_locks[task_id] = lock
    return lock


def task_round_lock(task_id: int) -> asyncio.Lock:
    """一轮回复（SSE 流全程）持有的长临界区锁。"""
    lock = _round_locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _round_locks[task_id] = lock
    return lock
