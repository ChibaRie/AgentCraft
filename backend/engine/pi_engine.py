"""PiEngine：单个 Pi 任务容器的协议封装（Engineering Spec §7.2/§7.3）。

职责：JSONL 读写、pending id/Future 关联、行解析三分叉分发
（response → Future；extension_ui_request → 自动应答；事件 → 回调）。
传输无关：Docker attach 与本地子进程都只需实现
`write_line(str) / readline() -> str | None / close()`。

协议纪律（rpc.md 0.84.3 实测帧序，见 tests/fixtures/pi_frames/）：
- prompt 的 response 只是受理 ACK；完成只认 agent_settled 事件（§12 决策 #6）
- stdin 写入经 writer lock 串行，JSONL 帧不交叉；abort 同样经 writer lock
- stdout 由独立协程持续排空，不读 = Pi 背压阻塞 = agent 冻结（§7.2 表）
- response id 不在 pending 表 = 容器状态污染 → needs_rebuild，由 manager 重建
- 单行 JSON 解析失败记日志跳过；EOF 交 manager 崩溃恢复
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Protocol

from backend.engine.docker_transport import MAX_LINE_BYTES

logger = logging.getLogger("agentcraft")

# extension_ui_request 应答时限（§3.2：必须在 2s 内应答，否则扩展调用挂起）
UI_RESPONSE_DEADLINE_SECONDS = 2.0


class PiEngineError(Exception):
    """Pi 引擎协议层错误基类。"""


class PiCommandTimeoutError(PiEngineError):
    """命令在时限内未收到 response 帧（§7.3：默认 30s）。"""


class PiTransport(Protocol):
    """Pi 进程行传输协议（Docker attach / 本地子进程各自适配）。"""

    async def write_line(self, line: str) -> None: ...

    async def readline(self) -> str | None:
        """返回一行（不含换行）；EOF 返回 None。"""
        ...

    async def close(self) -> None: ...


class PiEngine:
    """Pi RPC 进程的 Python 封装：attach、JSONL 读写、事件分发。"""

    def __init__(
        self,
        task_id: int,
        transport: PiTransport,
        *,
        command_timeout: float = 30.0,
    ) -> None:
        self.task_id = task_id
        self._transport = transport
        self._command_timeout = command_timeout
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._writer_lock = asyncio.Lock()
        self._event_callbacks: list = []
        self._reader_task: asyncio.Task | None = None
        # 本连接累计丢弃的不可解析 stdout 行数（红线 §4.7：只记长度与计数，不记正文）
        self._unparsable_dropped = 0
        self.needs_rebuild = False
        # 容器启动时的 Provider 指纹（ensure_container 写入，§7.7 指纹判定）
        self.provider_fingerprint = ""
        # 完成信号只认 agent_settled（§12 决策 #6）：轮处理器以此判断轮结束
        self.is_round_settled = True  # 初始无轮，视为已收尾
        # 容器播种时已写入 system prompt manifest 的附件 id（_create_engine 写入）；
        # 之后的轮次里出现的新 id → 运行中补传的附件，需在消息中主动告知
        self.seeded_file_ids: set[int] = set()

    # -- 生命周期 -----------------------------------------------------------

    async def start(self) -> None:
        """启动 stdout 排空协程（必须先于任何命令，§7.2 stdout 纪律）。"""
        if self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"pi-reader-task-{self.task_id}"
        )

    async def stop(self) -> None:
        """关闭传输并回收读取协程（容器删除由 manager 负责）。"""
        try:
            await self._transport.close()
        finally:
            if self._reader_task is not None:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾兜底
                    pass
                self._reader_task = None
            self._fail_pending(PiEngineError("engine stopped"))

    # -- 命令 ---------------------------------------------------------------

    async def send_command(self, cmd: dict, timeout: float | None = None) -> dict:
        """写入命令并等待同 id response 帧；超时抛 PiCommandTimeoutError。"""
        self._seq += 1
        cmd = {**cmd, "id": f"req_{self._seq}"}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[cmd["id"]] = future
        try:
            async with self._writer_lock:
                await self._transport.write_line(json.dumps(cmd, ensure_ascii=False))
            return await asyncio.wait_for(
                future, timeout=self._command_timeout if timeout is None else timeout
            )
        except asyncio.TimeoutError as exc:
            raise PiCommandTimeoutError(f"命令 {cmd.get('type')} 在时限内未收到 response") from exc
        finally:
            self._pending.pop(cmd["id"], None)

    async def send_prompt(self, message: str) -> dict:
        """发送用户消息，返回受理 ACK。完成以 agent_settled 为准，此处不等待。"""
        self.is_round_settled = False  # 消息轮开始置位（§7.2 活动轮状态）
        return await self.send_command({"type": "prompt", "message": message})

    async def abort(self) -> None:
        """写入 abort 帧并重置 settled 标记；不等待 ACK。绕过 mutation lock
        由 manager 保证，writer lock 保证 JSONL 不交叉（§7.2 控制命令）。"""
        self.is_round_settled = False
        async with self._writer_lock:
            await self._transport.write_line(json.dumps({"type": "abort"}))

    def on_event(self, callback) -> Callable[[], None]:
        """订阅事件帧；返回退订函数（轮处理器每轮订阅/退订，防回调堆积）。"""
        self._event_callbacks.append(callback)

        def unsubscribe() -> None:
            try:
                self._event_callbacks.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    # -- 行解析（三分叉）------------------------------------------------------

    async def handle_line(self, line: str) -> None:
        """分发单行 stdout：response / extension_ui_request / 事件。"""
        stripped = line.strip()
        if not stripped:
            return
        try:
            frame = json.loads(stripped)
        except json.JSONDecodeError:
            # 红线（§4.7）：坏行可能是 prompt/Provider 响应片段，只记长度与丢弃计数
            self._unparsable_dropped += 1
            logger.warning(
                "Task %s: 无法解析的 stdout 行已跳过（%d 字符，累计丢弃 %d 行）",
                self.task_id,
                len(line),
                self._unparsable_dropped,
            )
            return

        frame_type = frame.get("type")
        if frame_type == "response":
            self._handle_response(frame)
            return
        if frame_type == "extension_ui_request":
            asyncio.get_running_loop().call_later(
                0, lambda: asyncio.create_task(self._auto_respond_ui(frame))
            )
            return
        if frame_type == "agent_settled":
            self.is_round_settled = True
        await self._dispatch_event(frame)

    def _handle_response(self, frame: dict) -> None:
        frame_id = frame.get("id")
        if frame_id is None:
            # 无 id 的 ACK（如 abort），无需关联
            return
        future = self._pending.pop(frame_id, None)
        if future is None:
            # id 错配 = 容器状态被污染（§7.2.1），交 manager 重建
            logger.error("Task %s: response id %s 无关联命令，标记容器重建", self.task_id, frame_id)
            self.needs_rebuild = True
            return
        if not future.done():
            future.set_result(frame)

    async def _auto_respond_ui(self, frame: dict) -> None:
        """v1 自动应答扩展 UI：confirm→false，select/input/editor→cancelled（§3.2）。"""
        method = frame.get("method")
        if method == "confirm":
            payload: dict = {"confirmed": False}
        elif method in ("select", "input", "editor"):
            payload = {"cancelled": True}
        else:
            return  # notify/setStatus/setTitle/set_editor_text/setWidget：fire-and-forget
        response = {"type": "extension_ui_response", "id": frame.get("id"), **payload}
        try:
            async with self._writer_lock:
                await self._transport.write_line(json.dumps(response))
        except Exception:  # noqa: BLE001 - 应答失败不应拖垮读取循环
            logger.exception("Task %s: extension_ui_response 写入失败", self.task_id)

    async def _dispatch_event(self, frame: dict) -> None:
        for callback in self._event_callbacks:
            try:
                await callback(frame)
            except Exception:  # noqa: BLE001 - 单个回调失败不阻塞其余订阅者
                logger.exception("Task %s: 事件回调异常", self.task_id)

    async def _read_loop(self) -> None:
        """持续排空 stdout（背压防线）；EOF 静默退出，交 manager 崩溃恢复。"""
        try:
            while True:
                line = await self._transport.readline()
                if line is None:
                    logger.info("Task %s: stdout EOF，容器已退出", self.task_id)
                    self._fail_pending(PiEngineError("container exited"))
                    return
                if len(line) > MAX_LINE_BYTES:
                    logger.error(
                        "Task %s: stdout 行超长（%d 字符），已丢弃", self.task_id, len(line)
                    )
                    continue
                await self.handle_line(line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 读取循环异常交 manager 崩溃恢复
            logger.exception("Task %s: stdout 读取循环异常", self.task_id)
            self._fail_pending(PiEngineError("reader crashed"))

    def _fail_pending(self, error: PiEngineError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
