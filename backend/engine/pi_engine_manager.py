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
from pathlib import Path

from backend.config import Settings
from backend.engine.docker_transport import (
    DockerApiTransport,
    DockerCliTransport,
    build_container_spec,
    docker_ensure_network,
    docker_ensure_proxy_container,
    docker_remove_container,
)
from backend.engine.event_handler import EventHandler
from backend.engine.extension_generator import ExtensionGenerator
from backend.engine.pi_engine import PiEngine
from backend.engine.skill_loader import SkillLoader
from backend.engine.subprocess_transport import SubprocessPiTransport, resolve_pi_cli_js
from backend.services.provider_service import provider_fingerprint
from backend.services.task_token import create_task_token

logger = logging.getLogger("agentcraft")

HistoryFetcher = Callable[[int, int], Awaitable[list[dict]]]
# (user_id, provider_config_id) -> 当前生效 Provider 快照（§7.7 双模式回退链）
ProviderResolver = Callable[[int, int | None], Awaitable[dict]]

_ROLE_LABELS = {"user": "用户", "assistant": "助手", "tool": "工具"}

# 轮事件队列上限：text_delta 洪泛时的内存防线（超出部分丢弃流式增量）
_ROUND_QUEUE_MAX = 2000


class EngineStateError(Exception):
    """容器/引擎状态类错误（轮处理器转为 SSE error 帧）。"""


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
    ) -> None:
        self._settings = settings
        self._history_fetcher = history_fetcher
        self._provider_resolver = provider_resolver
        self._extension_generator = extension_generator
        self._skill_loader = skill_loader or SkillLoader(
            max_bytes=settings.SKILL_PROMPT_MAX_BYTES
        )
        self._task_files_root = (
            task_files_root
            if task_files_root is not None
            else (Path(settings.HOST_DATA_ROOT) / "task-files").resolve()
        )
        self._containers: dict[int, PiEngine] = {}
        self._removal_hooks: dict[int, Callable[[], Awaitable[None]]] = {}
        self._task_tokens: dict[int, str] = {}

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
        healthy = (
            engine is not None
            and not engine.needs_rebuild
            and engine._reader_task is not None
            and not engine._reader_task.done()
        )
        if healthy and engine.provider_fingerprint != current_fingerprint:
            logger.info(
                "Task %s: Provider 指纹变化（%s → %s），重建容器",
                task_id,
                engine.provider_fingerprint,
                current_fingerprint,
            )
            healthy = False
        if healthy:
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
        await transport.start()
        await engine.start()
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

    async def _make_runtime(self, spec, workdir_host: Path, extension_path: Path):
        """按 PI_RUNTIME 选择传输：docker(API) / cli / subprocess；auto 依序回退。"""
        runtime = self._settings.PI_RUNTIME
        if runtime in ("auto", "docker"):
            transport = await self._try_docker_api(spec)
            if transport is not None:
                return transport, self._api_removal(spec.container_name)
            if runtime == "docker":
                raise EngineStateError(
                    f"Docker API 不可达（{self._settings.DOCKER_API_URL}）"
                )
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
            SubprocessPiTransport(
                argv, cwd=workdir_host, env={**os.environ, **spec.env}
            ),
            self._noop_removal,
        )

    async def _try_docker_api(self, spec):
        try:
            import aiodocker

            docker = aiodocker.Docker(url=self._settings.DOCKER_API_URL)
            await docker.system.info()
            return DockerApiTransport(docker, spec)
        except Exception as exc:  # noqa: BLE001 - 探测失败即回退
            logger.info("Docker API 探测失败: %s", exc)
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

        handler = EventHandler(persist_assistant, persist_tool)
        # 有界队列：慢消费时丢弃可再生的流式增量（done/message_saved/tool_event/
        # error 等关键事件必须送达，宁可短暂阻塞读取协程形成背压）
        queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=_ROUND_QUEUE_MAX)

        async def on_frame(frame: dict) -> None:
            for sse_event in await handler.handle_frame(frame):
                if queue.qsize() >= _ROUND_QUEUE_MAX:
                    if sse_event[0] in ("text_delta", "thinking_delta"):
                        logger.warning("Task %s: 轮队列已满，丢弃流式增量帧", task_id)
                        continue
                await queue.put(sse_event)

        unsubscribe = engine.on_event(on_frame)
        try:
            message = await self._build_outgoing_message(engine, task_id, content)
            await engine.send_prompt(message)
            engine.needs_reseed = False
            # §7.8 轮超时以整轮为限：deadline 一次计算，逐次扣减剩余时间，
            # 防止慢速事件流把每段等待都重置成完整超时（DoS 加长轮占用）
            deadline = asyncio.get_running_loop().time() + self._settings.PI_ROUND_TIMEOUT_SECONDS
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self._round_timeout(task_id, engine)
                    yield (
                        "error",
                        {
                            "code": "ROUND_TIMEOUT",
                            "message": "回复超时，请重试",
                            "recoverable": True,
                        },
                    )
                    yield (
                        "done",
                        {
                            "finish_reason": "aborted",
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                        },
                    )
                    return
                try:
                    name, payload = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    await self._round_timeout(task_id, engine)
                    logger.error("Task %s: 轮超时，已发送 abort", task_id)
                    yield (
                        "error",
                        {
                            "code": "ROUND_TIMEOUT",
                            "message": "回复超时，请重试",
                            "recoverable": True,
                        },
                    )
                    yield (
                        "done",
                        {
                            "finish_reason": "aborted",
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                        },
                    )
                    return
                yield (name, payload)
                if name == "done":
                    if engine.needs_rebuild:
                        # id 错配在轮内暴露：本轮已尽力收尾，标记下次重建
                        logger.error("Task %s: 检测到 response id 错配，下轮重建容器", task_id)
                    return
        finally:
            unsubscribe()

    async def _round_timeout(self, task_id: int, engine: PiEngine) -> None:
        """§7.8：无 agent_settled 超时兜底 → abort → 可重试错误。"""
        await engine.abort()
        logger.error("Task %s: 轮超时，已发送 abort", task_id)

    async def _build_outgoing_message(
        self, engine: PiEngine, task_id: int, content: str
    ) -> str:
        """重播种：历史窗口嵌入本条消息开头；正常轮只发当前消息（§7.6）。"""
        if not engine.needs_reseed:
            return content
        history = await self._history_fetcher(
            task_id, self._settings.MAX_HISTORY_MESSAGES
        )
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
