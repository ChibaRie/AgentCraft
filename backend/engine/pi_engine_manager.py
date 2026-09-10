"""PiEngineManager：容器池与消息轮编排（Engineering Spec §7.2/§7.6，阶段 5 最小串行版）。

职责边界（阶段 5）：
- 容器表 {task_id: PiEngine}，ensure_container 惰性创建；needs_rebuild（id 错配）
  或引擎死亡时重建
- 重播种（§7.6，连续性核心）：新容器的第一条消息把最近 MAX_HISTORY_MESSAGES=40
  条持久化历史嵌入消息开头，绝不单独发历史（防幻影轮）；正常轮只发当前消息
- run_round：容器 → 重播种拼装 → send_prompt → EventHandler 翻译为 SSE 事件流，
  以 done 收尾；轮超时（PI_ROUND_TIMEOUT_SECONDS）兜底 abort
- request_abort 绕过 mutation lock（writer lock 保证 JSONL 不交叉，§7.2.1）
- 任务令牌：每容器启动生成，随重建失效（阶段 6 MCP 桥使用）

明确不做（阶段 7）：并发上限排队、空闲回收、崩溃恢复重试 3 次、Skill 指纹
kill switch、看门狗巡检。
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from backend.config import Settings
from backend.engine.docker_transport import (
    DockerApiTransport,
    DockerCliTransport,
    build_container_spec,
    docker_ensure_backend_forwarder,
    docker_ensure_network,
    docker_ensure_proxy_container,
    docker_remove_container,
)
from backend.engine.event_handler import EventHandler
from backend.engine.extension_generator import ExtensionGenerator
from backend.engine.pi_engine import PiEngine, PiEngineError
from backend.engine.skill_loader import SkillLoader
from backend.engine.subprocess_transport import SubprocessPiTransport, resolve_pi_cli_js
from backend.services.provider_service import provider_fingerprint
from backend.services.task_token import create_task_token

logger = logging.getLogger("agentcraft")

HistoryFetcher = Callable[[int, int], Awaitable[list[dict]]]
# (user_id, provider_config_id) -> 当前生效 Provider 快照（§7.7 双模式回退链）
ProviderResolver = Callable[[int, int | None], Awaitable[dict]]

_ROLE_LABELS = {"user": "用户", "assistant": "助手", "tool": "工具"}


def _human_bytes(num_bytes: int) -> str:
    """附件尺寸的人类可读格式（仅用于提示词展示）。"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


# 轮事件队列上限：text_delta 洪泛时的内存防线（超出部分丢弃流式增量）
_ROUND_QUEUE_MAX = 2000

# 后台巡检节奏（§7.2 空闲回收 + §7.8.1 看门狗共用一个循环）
_BACKGROUND_SWEEP_SECONDS = 30

# §7.8 崩溃恢复：容器重建+重播种重发的最大次数
_CRASH_RECOVERY_ATTEMPTS = 3

# §7.8 轮超时/崩溃收尾的固定 SSE 帧（§6.6 done 枚举仅 stop|aborted）
_ROUND_TIMEOUT_ERROR = {
    "code": "ROUND_TIMEOUT",
    "message": "回复超时，请重试",
    "recoverable": True,
}
_ABORTED_DONE = {
    "finish_reason": "aborted",
    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
}
_ENGINE_CRASHED_ERROR = {
    "code": "ENGINE_CRASHED",
    "message": "引擎连续崩溃，请重试",
    "recoverable": True,
}


class EngineStateError(Exception):
    """容器/引擎状态类错误（轮处理器转为 SSE error 帧）。"""


class EngineCrashed(Exception):
    """轮进行中容器死亡（reader EOF）——触发 §7.8 崩溃恢复。"""


class PiEngineManager:
    """每任务一容器：创建、重建、重播种、轮编排、abort。"""

    def __init__(
        self,
        settings: Settings,
        *,
        history_fetcher: HistoryFetcher,
        extension_generator: ExtensionGenerator,
        provider_resolver: ProviderResolver,
        skill_loader: SkillLoader | None = None,
        task_files_root: Path | None = None,
        running_tasks_fetcher: Callable[[], Awaitable[list[dict]]] | None = None,
        mark_task_failed: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._history_fetcher = history_fetcher
        self._provider_resolver = provider_resolver
        self._extension_generator = extension_generator
        # §7.8.1 看门狗的 DB 触点（dependencies 注入；测试可替换）
        self._running_tasks_fetcher = running_tasks_fetcher
        self._mark_task_failed = mark_task_failed
        self._skill_loader = skill_loader or SkillLoader(max_bytes=settings.SKILL_PROMPT_MAX_BYTES)
        self._task_files_root = (
            task_files_root
            if task_files_root is not None
            else (Path(settings.HOST_DATA_ROOT) / "task-files").resolve()
        )
        self._containers: dict[int, PiEngine] = {}
        self._removal_hooks: dict[int, Callable[[], Awaitable[None]]] = {}
        self._task_tokens: dict[int, str] = {}
        # §7.2 并发上限：容器槽位信号量（holder 集合保证一一配对释放）
        self._slots = asyncio.Semaphore(max(1, settings.PI_MAX_CONCURRENT_CONTAINERS))
        self._slot_holders: set[int] = set()
        # §7.2 空闲回收：每任务最近活动时刻（loop.monotonic）
        self._last_activity: dict[int, float] = {}
        self._background_task: asyncio.Task | None = None

    # -- 容器生命周期 ---------------------------------------------------------

    async def ensure_container(
        self,
        *,
        task_id: int,
        user_id: int,
        provider_config_id: int | None,
        stored_workdir: str,
        skill_snapshot: dict,
        task_files: list[dict],
        expert_name: str,
        mcp_tools: list[dict] | None = None,
    ) -> PiEngine:
        """取用健康容器；Skill/Provider 指纹变化或引擎死亡时重建。

        Provider 指纹（§7.7）：当前生效配置（新鲜解析，回退链=显式→用户默认→
        系统）vs 容器启动时指纹。DB 的 provider_snapshot 保持冻结（历史事实源），
        容器级配置按最新解析值生效——改配置不影响进行中一轮，下一轮重建生效。
        """
        current_provider = await self._provider_resolver(user_id, provider_config_id)
        current_fingerprint = provider_fingerprint(current_provider)

        engine = self._containers.get(task_id)
        healthy = self._engine_healthy(task_id)
        if healthy and engine.provider_fingerprint != current_fingerprint:
            logger.info(
                "Task %s: Provider 指纹变化（%s → %s），重建容器",
                task_id,
                engine.provider_fingerprint,
                current_fingerprint,
            )
            healthy = False
        if healthy:
            self._last_activity[task_id] = asyncio.get_running_loop().time()
            return engine
        if engine is not None:
            await self._teardown(task_id)
        engine = await self._create_engine(
            task_id=task_id,
            stored_workdir=stored_workdir,
            skill_snapshot=skill_snapshot,
            task_files=task_files,
            expert_name=expert_name,
            mcp_tools=mcp_tools or [],
            provider_snapshot=current_provider,
        )
        engine.provider_fingerprint = current_fingerprint
        self._containers[task_id] = engine
        return engine

    def _engine_healthy(self, task_id: int) -> bool:
        """容器存在、未被标记重建、reader 协程存活。"""
        engine = self._containers.get(task_id)
        return (
            engine is not None
            and not engine.needs_rebuild
            and engine._reader_task is not None
            and not engine._reader_task.done()
        )

    async def _create_engine(
        self,
        *,
        task_id: int,
        stored_workdir: str,
        skill_snapshot: dict,
        task_files: list[dict],
        expert_name: str,
        mcp_tools: list[dict],
        provider_snapshot: dict,
    ) -> PiEngine:
        system_prompt = self._skill_loader.build_system_prompt(
            skill_snapshot, task_files, expert_name=expert_name
        )
        # §7.2 并发上限：容器占用一个槽位（teardown 释放）；满时在此排队
        if task_id not in self._slot_holders:
            await self._slots.acquire()
            self._slot_holders.add(task_id)
        # 容器 argv 用当前解析的 Provider（协议/模型）；base_url 与 Key 由
        # provider-proxy 按任务令牌侧解析（真实 Key 永不进容器，§7.7）
        provider = provider_snapshot.get("protocol") or self._settings.PI_PROVIDER
        model_id = provider_snapshot.get("model_id") or self._settings.PI_MODEL
        workdir_host = self._resolve_workdir_host(stored_workdir)
        task_files_host = self._task_files_root / f"task-{task_id}"
        task_files_host.mkdir(parents=True, exist_ok=True)  # 空目录也需可 bind
        extension_path = self._extension_generator.generate(task_id, mcp_tools, provider)
        # §7.9：挂载源全部服务端派生且必须绝对化
        workdir_host = self._absolute(workdir_host, "工作目录")
        task_files_host = self._absolute(task_files_host, "任务文件目录")
        extension_path = self._absolute(extension_path, "扩展文件")

        # JWT 任务令牌：proxy 无状态校验（task_id/model scope），instance 随容器轮换
        task_token = create_task_token(
            task_id,
            instance=secrets.token_hex(8),
            model_id=provider_snapshot.get("model_id") or self._settings.PI_MODEL,
        )
        self._task_tokens[task_id] = task_token

        spec = build_container_spec(
            task_id=task_id,
            image=self._settings.PI_WORKER_IMAGE,
            provider=provider,
            model=model_id,
            system_prompt=system_prompt,
            workdir_host=workdir_host,
            task_files_host=task_files_host,
            extension_host=extension_path,
            task_token=task_token,
            backend_url=self._settings.AGENTCRAFT_BACKEND_URL,
            network_name=self._settings.PI_NETWORK_NAME,
            faux_chunk_delay_ms=(
                self._settings.PI_FAUX_CHUNK_DELAY_MS if provider == "faux" else 0
            ),
        )

        if provider != "faux":
            await self.ensure_proxy(provider)
        transport, removal = await self._make_runtime(spec, workdir_host, extension_path)
        self._removal_hooks[task_id] = removal

        engine = PiEngine(task_id, transport, command_timeout=30.0)
        engine.needs_reseed = True  # 新容器内存为空，首条消息必须重播种（§7.6）
        # system prompt manifest 已含当前全部附件（§6.6）：播种集合 = 全量 id，
        # 之后轮次出现的新 id 才是运行中补传，需在消息里告知
        engine.seeded_file_ids = {item["id"] for item in task_files}
        await transport.start()
        await engine.start()
        self._last_activity[task_id] = asyncio.get_running_loop().time()
        logger.info("Task %s: Pi 引擎就绪（%s）", task_id, type(transport).__name__)
        return engine

    async def ensure_proxy(self, provider: str) -> None:
        """非 faux Provider 需要 provider-proxy 容器（internal 网络内可达）。"""
        if provider == "faux" or not shutil.which("docker"):
            return
        try:
            await docker_ensure_proxy_container(
                image=self._settings.PROVIDER_PROXY_IMAGE,
                container_name="provider-proxy",
                network_name=self._settings.PI_NETWORK_NAME,
                app_dir=Path(__file__).resolve().parents[2],
            )
        except Exception:  # noqa: BLE001 - proxy 启动失败不阻塞 faux/容器创建
            logger.exception("Provider Proxy 容器保障失败")
        # dev 形态（控制面在宿主机）：确保容器可回调 /internal/mcp/call（MCP 桥）。
        # compose 形态函数内自动跳过；faux 无工具调用无需回调
        try:
            await docker_ensure_backend_forwarder(
                network_name=self._settings.PI_NETWORK_NAME,
                image=self._settings.PI_WORKER_IMAGE,
                target_port=self._settings.AGENTCRAFT_BACKEND_PORT,
            )
        except Exception:  # noqa: BLE001 - 转发器失败不阻塞容器创建
            logger.exception("后端转发容器保障失败")

    async def _make_runtime(self, spec, workdir_host: Path, extension_path: Path):
        """按 PI_RUNTIME 选择传输：docker(API) / cli / subprocess；auto 依序回退。"""
        runtime = self._settings.PI_RUNTIME
        if runtime in ("auto", "docker"):
            transport = await self._try_docker_api(spec)
            if transport is not None:
                return transport, self._api_removal(spec.container_name)
            if runtime == "docker":
                raise EngineStateError(f"Docker API 不可达（{self._settings.DOCKER_API_URL}）")
            logger.warning("Docker API 不可达，回退 docker CLI 传输")
        if runtime in ("auto", "cli"):
            if shutil.which("docker"):
                await docker_ensure_network(spec.network_name)
                return DockerCliTransport(spec), self._cli_removal(spec.container_name)
            if runtime == "cli":
                raise EngineStateError("docker CLI 不可用")
            logger.warning("docker CLI 不可用，回退本地子进程传输（无沙箱，仅开发）")
        cli_js = resolve_pi_cli_js()
        # 子进程形态：node <cli.js> + 同一 argv；容器内挂载路径替换为本地真实路径
        head = ["node", str(cli_js), *spec.argv[1:-2]]
        argv = [*head, "-e", str(extension_path)]
        return (
            SubprocessPiTransport(argv, cwd=workdir_host, env={**os.environ, **spec.env}),
            self._noop_removal,
        )

    async def _try_docker_api(self, spec):
        docker = None
        try:
            import aiodocker

            docker = aiodocker.Docker(url=self._settings.DOCKER_API_URL)
            await docker.system.info()
            return DockerApiTransport(docker, spec)
        except Exception as exc:  # noqa: BLE001 - 探测失败即回退
            logger.info("Docker API 探测失败: %s", exc)
            if docker is not None:
                try:
                    await docker.close()  # 探测失败的会话必须关闭（防 Unclosed 警告泄漏）
                except Exception:  # noqa: BLE001
                    pass
            return None

    def _api_removal(self, container_name: str) -> Callable[[], Awaitable[None]]:
        async def remove() -> None:
            try:
                import aiodocker

                docker = aiodocker.Docker(url=self._settings.DOCKER_API_URL)
                await docker.containers.delete(container_name, force=True)
            except Exception:  # noqa: BLE001 - 尽力删除
                logger.warning("容器 %s API 删除失败（可能已退出）", container_name)

        return remove

    def _cli_removal(self, container_name: str) -> Callable[[], Awaitable[None]]:
        async def remove() -> None:
            await docker_remove_container(container_name)

        return remove

    def _noop_removal(self) -> Awaitable[None]:
        async def noop() -> None:
            return None

        return noop

    def _resolve_workdir_host(self, stored_workdir: str) -> Path:
        """tasks.workdir（/workspaces/authorized[/rel]）→ 主机目录。"""
        root = Path(self._settings.HOST_WORKSPACE_ROOT).resolve()
        canonical = self._settings.AGENTCRAFT_WORKSPACE_ROOT
        relative = stored_workdir.removeprefix(canonical).strip("/")
        target = (root / relative).resolve() if relative else root
        if target != root and root not in target.parents:
            raise EngineStateError(f"工作目录越界: {stored_workdir}")
        return target

    def resolve_workdir_host(self, stored_workdir: str) -> Path:
        """公开包装（harness 内部接口解析任务 workdir 用）。"""
        return self._resolve_workdir_host(stored_workdir)

    @staticmethod
    def _absolute(path: Path, label: str) -> Path:
        """bind mount source 必须是绝对路径（docker CLI 相对路径直接报错）。"""
        resolved = Path(path).resolve()
        if not resolved.is_absolute():
            raise EngineStateError(f"{label} 不是绝对路径: {path}")
        return resolved

    async def _teardown(self, task_id: int) -> None:
        engine = self._containers.pop(task_id, None)
        if engine is not None:
            await engine.stop()
        removal = self._removal_hooks.pop(task_id, None)
        if removal is not None:
            await removal()
        self._task_tokens.pop(task_id, None)
        if task_id in self._slot_holders:
            self._slot_holders.discard(task_id)
            self._slots.release()
        self._last_activity.pop(task_id, None)

    # -- 后台巡检（§7.2 空闲回收；§7.8.1 看门狗在 _background_loop 内扩展） ----

    def ensure_background(self) -> None:
        """启动后台巡检循环（lifespan 调用；重复调用幂等）。"""
        if self._background_task is None or self._background_task.done():
            self._background_task = asyncio.create_task(self._background_loop())

    async def _background_loop(self) -> None:
        while True:
            await asyncio.sleep(_BACKGROUND_SWEEP_SECONDS)
            try:
                await self.sweep_idle()
            except Exception:  # noqa: BLE001 - 巡检失败不终止循环
                logger.exception("空闲回收巡检失败")
            try:
                await self.sweep_watchdog()
            except Exception:  # noqa: BLE001 - 巡检失败不终止循环
                logger.exception("看门狗巡检失败")

    async def sweep_idle(self) -> int:
        """空闲回收（§7.2）：连续 PI_IDLE_TIMEOUT_MINUTES 无活动且无活动轮
        的容器 → 回收（下条消息自动重建+重播种）。返回回收数。"""
        now = asyncio.get_running_loop().time()
        idle_seconds = self._settings.PI_IDLE_TIMEOUT_MINUTES * 60
        reclaimed = 0
        for task_id, engine in list(self._containers.items()):
            last = self._last_activity.get(task_id)
            if last is None or not engine.is_round_settled:
                continue
            if now - last <= idle_seconds:
                continue
            logger.info(
                "Task %s: 空闲超过 %d 分钟，回收容器",
                task_id,
                self._settings.PI_IDLE_TIMEOUT_MINUTES,
            )
            await self._teardown(task_id)
            reclaimed += 1
        return reclaimed

    async def sweep_watchdog(self) -> int:
        """看门狗巡检（§7.8.1）：确保不存在「running 但无终态保障」的任务。

        - 容器死亡（无活动轮）→ 回收 + 标记 failed（可重试）
        - running 累计时长超 PI_TASK_MAX_LIFETIME_MINUTES → abort（若在轮中）
          + 回收容器 + 标记 failed（可重试）
        返回处置数。DB 触点经注入的 fetcher/marker（单测替换）。
        """
        if self._running_tasks_fetcher is None:
            return 0
        rows = await self._running_tasks_fetcher()
        now = datetime.now(timezone.utc)
        lifetime_limit = self._settings.PI_TASK_MAX_LIFETIME_MINUTES * 60
        handled = 0
        for row in rows:
            task_id = row["id"]
            running_since = row.get("running_since")
            engine = self._containers.get(task_id)
            # 1) 容器死亡（无活动轮）：崩溃恢复只覆盖轮内；轮间死亡在此收口
            if engine is not None and not self._engine_healthy(task_id):
                if not engine.is_round_settled:
                    continue  # 活动轮中的容器死亡由 run_round 崩溃恢复负责
                logger.warning("Task %s: 看门狗发现容器死亡，回收并标记 failed", task_id)
                await self._teardown(task_id)
                await self._mark_failed_safe(task_id)
                handled += 1
                continue
            # 2) 任务总超时（§7.8.1）：running 累计时长超阈
            if running_since is not None:
                started = (
                    running_since
                    if running_since.tzinfo is not None
                    else running_since.replace(tzinfo=timezone.utc)
                )
                if (now - started).total_seconds() <= lifetime_limit:
                    continue
                logger.warning("Task %s: 任务总超时，abort + failed + 回收容器", task_id)
                if engine is not None:
                    if not engine.is_round_settled:
                        await self.request_abort(task_id)
                    await self._teardown(task_id)
                await self._mark_failed_safe(task_id)
                handled += 1
        return handled

    async def _mark_failed_safe(self, task_id: int) -> None:
        if self._mark_task_failed is not None:
            await self._mark_task_failed(task_id)

    async def stop_container(self, task_id: int) -> None:
        """停止并删除容器、失效任务令牌（任务删除/complete 用）。"""
        await self._teardown(task_id)

    def has_active_round(self, task_id: int) -> bool:
        """complete preflight / abort 判断用（§7.2 活动轮状态）。"""
        engine = self._containers.get(task_id)
        return engine is not None and not engine.is_round_settled

    async def request_abort(self, task_id: int) -> None:
        """用户中止：绕过 task mutation lock 直接写 abort 帧（§7.2.1）。"""
        engine = self._containers.get(task_id)
        if engine is None:
            return
        if not engine.is_round_settled:
            await engine.abort()
            logger.info("Task %s: 已发送 abort", task_id)

    def get_task_token(self, task_id: int) -> str | None:
        """当前容器实例的任务令牌（阶段 6 /internal/mcp/call 校验用）。"""
        return self._task_tokens.get(task_id)

    # -- 消息轮 --------------------------------------------------------------

    async def run_round(
        self,
        *,
        task_id: int,
        user_id: int,
        provider_config_id: int | None = None,
        stored_workdir: str,
        skill_snapshot: dict,
        task_files: list[dict],
        expert_name: str,
        mcp_tools: list[dict] | None = None,
        content: str,
        persist_assistant: Callable,
        persist_tool: Callable,
    ) -> AsyncIterator[tuple[str, dict]]:
        """执行一轮：容器（含 Provider 指纹判定）→ 重播种 → prompt → SSE 事件直到 done。

        调用方（API 层）已持有轮锁并完成 prepare_send（用户消息已落库）。
        """
        # §7.2 并发上限：需要新建容器且槽已满 → 先推 queued 再排队等待
        if not self._engine_healthy(task_id) and self._slots.locked():
            yield ("queued", {})
        engine = await self.ensure_container(
            task_id=task_id,
            user_id=user_id,
            provider_config_id=provider_config_id,
            stored_workdir=stored_workdir,
            skill_snapshot=skill_snapshot,
            task_files=task_files,
            expert_name=expert_name,
            mcp_tools=mcp_tools,
        )
        self._last_activity[task_id] = asyncio.get_running_loop().time()

        # §7.8 崩溃恢复：容器在轮中死亡 → 重建 + 重播种重发，最多 3 次；
        # 仍失败 → 标记 failed（可重试）并以 error 帧收尾
        for attempt in range(1, _CRASH_RECOVERY_ATTEMPTS + 1):
            handler = EventHandler(persist_assistant, persist_tool)
            queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=_ROUND_QUEUE_MAX)
            try:
                async for event in self._stream_attempt(
                    engine,
                    queue,
                    handler,
                    task_id=task_id,
                    content=content,
                    task_files=task_files,
                ):
                    yield event
                return
            except (EngineCrashed, PiEngineError) as exc:
                await self._teardown(task_id)
                if attempt >= _CRASH_RECOVERY_ATTEMPTS:
                    logger.error("Task %s: 崩溃恢复耗尽（%d 次）: %s", task_id, attempt, exc)
                    if self._mark_task_failed is not None:
                        await self._mark_task_failed(task_id)
                    yield ("error", _ENGINE_CRASHED_ERROR)
                    yield ("done", _ABORTED_DONE)
                    return
                logger.warning(
                    "Task %s: 容器崩溃（第 %d/%d 次），重建并重播种: %s",
                    task_id,
                    attempt,
                    _CRASH_RECOVERY_ATTEMPTS,
                    exc,
                )
                engine = await self.ensure_container(
                    task_id=task_id,
                    user_id=user_id,
                    provider_config_id=provider_config_id,
                    stored_workdir=stored_workdir,
                    skill_snapshot=skill_snapshot,
                    task_files=task_files,
                    expert_name=expert_name,
                    mcp_tools=mcp_tools,
                )
                self._last_activity[task_id] = asyncio.get_running_loop().time()

    async def _stream_attempt(
        self,
        engine: PiEngine,
        queue: asyncio.Queue,
        handler: EventHandler,
        *,
        task_id: int,
        content: str,
        task_files: list[dict],
    ) -> AsyncIterator[tuple[str, dict]]:
        """单次尝试：发消息并转发事件至 done/超时收尾；容器死亡抛 EngineCrashed。"""

        async def on_frame(frame: dict) -> None:
            for sse_event in await handler.handle_frame(frame):
                if queue.qsize() >= _ROUND_QUEUE_MAX:
                    if sse_event[0] in ("text_delta", "thinking_delta"):
                        logger.warning("Task %s: 轮队列已满，丢弃流式增量帧", task_id)
                        continue
                await queue.put(sse_event)

        unsubscribe = engine.on_event(on_frame)
        reader = engine._reader_task
        try:
            message = await self._build_outgoing_message(engine, task_id, content, task_files)
            await engine.send_prompt(message)
            engine.needs_reseed = False
            # §7.8 轮超时以整轮为限：deadline 一次计算，逐次扣减剩余时间，
            # 防止慢速事件流把每段等待都重置成完整超时（DoS 加长轮占用）
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._settings.PI_ROUND_TIMEOUT_SECONDS
            while True:
                outcome = await self._next_round_event(queue, reader, deadline, task_id, engine)
                if outcome == "timeout":
                    yield ("error", _ROUND_TIMEOUT_ERROR)
                    yield ("done", _ABORTED_DONE)
                    return
                name, payload = outcome
                yield (name, payload)
                if name == "done" and engine.needs_rebuild:
                    # id 错配在轮内暴露：本轮已尽力收尾，标记下次重建
                    logger.error("Task %s: 检测到 response id 错配，下轮重建容器", task_id)
                    return
                if name == "done":
                    return
        finally:
            unsubscribe()

    async def _next_round_event(self, queue, reader, deadline, task_id, engine):
        """等待下一个轮事件：事件 / "timeout"；容器死亡（reader EOF）抛
        EngineCrashed，不空等至整轮超时。"""
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return "timeout"
        waiter = asyncio.create_task(queue.get())
        assert reader is not None
        done_set, _pending = await asyncio.wait(
            {waiter, reader},
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if waiter in done_set:
            return waiter.result()
        waiter.cancel()
        if reader in done_set or reader.done():
            raise EngineCrashed(f"Task {task_id}: stdout EOF")
        await self._round_timeout(task_id, engine)
        logger.error("Task %s: 轮超时，已发送 abort", task_id)
        return "timeout"

    async def _round_timeout(self, task_id: int, engine: PiEngine) -> None:
        """§7.8：无 agent_settled 超时兜底 → abort → 可重试错误。"""
        await engine.abort()
        logger.error("Task %s: 轮超时，已发送 abort", task_id)

    async def _build_outgoing_message(
        self, engine: PiEngine, task_id: int, content: str, task_files: list[dict]
    ) -> str:
        """组装本轮发出的消息：重播种历史（§7.6）+ 运行中补传附件告知（§6.6）。"""
        if engine.needs_reseed:
            message = await self._reseed_message(engine, task_id, content)
        else:
            message = content
        notice = self._attachment_notice(engine, task_files)
        if notice:
            message = f"{message}\n\n{notice}"
        return message

    async def _reseed_message(self, engine: PiEngine, task_id: int, content: str) -> str:
        """重播种消息：最近历史嵌入本条消息开头（§7.6）；无历史则原样发送。"""
        history = await self._history_fetcher(task_id, self._settings.MAX_HISTORY_MESSAGES)
        # 当前用户消息已落库且为最后一条；回顾只包含它之前的历史
        prior = []
        if history and history[-1].get("role") == "user" and history[-1].get("content") == content:
            prior = history[:-1]
        else:
            prior = history
        if not prior:
            return content
        lines = ["[历史对话回顾]"]
        for item in prior:
            label = _ROLE_LABELS.get(item.get("role", ""), "消息")
            lines.append(f"{label}：{item.get('content', '')}")
        lines.append("")
        lines.append("[当前消息]")
        lines.append(content)
        logger.info("Task %s: 重播种 %d 条历史", task_id, len(prior))
        return "\n".join(lines)

    def _attachment_notice(self, engine: PiEngine, task_files: list[dict]) -> str:
        """运行中补传附件的告知块（§6.6）：不在容器播种集合内的文件，
        在消息末尾告知 Agent 经只读挂载 /task-files 读取；告知后并入播种集合。"""
        new_files = [item for item in task_files if item.get("id") not in engine.seeded_file_ids]
        if not new_files:
            return ""
        lines = [
            "[附件更新]",
            "用户在对话中上传了以下新文件（只读挂载 /task-files/，可用 read 工具查看）：",
        ]
        for item in new_files:
            lines.append(
                f"- {item.get('original_name', '')} → {item.get('agent_path', '')}"
                f"（{_human_bytes(int(item.get('size_bytes') or 0))}）"
            )
        engine.seeded_file_ids.update(item["id"] for item in new_files)
        logger.info("Task %s: 附件更新 %d 个新文件", engine.task_id, len(new_files))
        return "\n".join(lines)
