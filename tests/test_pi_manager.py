"""PiEngineManager 测试：容器表、重播种、abort、重建（§7.2/§7.6）。

用脚本化假 Pi 传输（prompt → 回显整条 outgoing 消息）验证重播种语义，
不依赖真实容器；真实 faux 全链路在验收阶段覆盖。
"""

import asyncio
from pathlib import Path

from backend.config import Settings
from backend.engine.extension_generator import ExtensionGenerator
from backend.engine.pi_engine_manager import PiEngineManager
from tests.conftest import FakePiTransport


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        PI_RUNTIME="subprocess",
        HOST_DATA_ROOT=str(tmp_path / "data"),
        HOST_WORKSPACE_ROOT=str(tmp_path / "workspaces"),
        PI_ROUND_TIMEOUT_SECONDS=5,
    )


class SpyPersistence:
    def __init__(self) -> None:
        self.assistant: list[dict] = []
        self.tools: list[dict] = []

    async def persist_assistant(self, content: str, usage: dict) -> dict:
        self.assistant.append({"content": content, "usage": usage})
        return {"message_id": len(self.assistant), "content": content}

    async def persist_tool(self, tool_call_id, tool_name, content, is_error) -> dict:
        self.tools.append({"tool_call_id": tool_call_id})
        return {"message_id": 999}


SNAPSHOT = {
    "skills": [{"name": "s", "content": "角色：测试"}],
    "expert_persona": "严谨",
    "expert_methodology": "分步",
    "loaded_at": "2026-09-03T00:00:00Z",
}


def make_manager(tmp_path: Path, history: list[dict]):
    settings = make_settings(tmp_path)
    transports: list[FakePiTransport] = []
    # 可编程 Provider 解析器：测试按序弹出快照（默认恒为 system）
    provider_snapshots: list[dict] = []

    async def resolve_provider(user_id: int, provider_config_id: int | None) -> dict:
        if provider_snapshots:
            return provider_snapshots.pop(0)
        return {"source": "system", "protocol": "openai",
                "base_url": "http://provider-proxy:8080/v1", "model_id": "gpt-4o-mini"}

    manager = PiEngineManager(
        settings,
        history_fetcher=_fake_history_fetcher(history),
        extension_generator=ExtensionGenerator(tmp_path / "ext"),
        provider_resolver=resolve_provider,
    )
    manager._removal = []  # type: ignore[attr-defined]
    manager._provider_snapshots = provider_snapshots  # type: ignore[attr-defined]

    async def fake_make_runtime(spec, workdir_host, extension_path):
        transport = FakePiTransport()
        transports.append(transport)

        async def noop() -> None:
            return None

        manager._removal.append(spec)  # type: ignore[attr-defined]
        return transport, noop

    manager._make_runtime = fake_make_runtime  # type: ignore[method-assign]
    return manager, transports


async def _fake_history_fetcher(history: list[dict], task_id: int, limit: int) -> list[dict]:
    return history[-limit:]


def _fake_history_fetcher(history: list[dict]):
    async def fetch(task_id: int, limit: int) -> list[dict]:
        return history[-limit:]

    return fetch


async def drive_round(
    manager: PiEngineManager, transports, content: str, *, task_files: list[dict] | None = None
):
    spy = SpyPersistence()
    events = []
    async for name, payload in manager.run_round(
        task_id=1,
        user_id=1,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=task_files or [],
        expert_name="周报管家",
        content=content,
        persist_assistant=spy.persist_assistant,
        persist_tool=spy.persist_tool,
    ):
        events.append((name, payload))
    return events, spy


# ---------------------------------------------------------------------------
# 重播种（§7.6）
# ---------------------------------------------------------------------------


async def test_first_round_wraps_history(tmp_path):
    history = [
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "第二轮新消息"},
    ]
    manager, transports = make_manager(tmp_path, history)
    events, spy = await drive_round(manager, transports, "第二轮新消息")

    outgoing = transports[0].written[0]["message"]
    assert outgoing.startswith("[历史对话回顾]")
    assert "用户：第一轮问题" in outgoing
    assert "助手：第一轮回答" in outgoing
    assert "[当前消息]\n第二轮新消息" in outgoing
    assert not transports[0].written[0].get("streamingBehavior")

    done = events[-1]
    assert done[0] == "done" and done[1]["finish_reason"] == "stop"
    # 幻影轮防线：一轮恰好一条 assistant 落库
    assert len(spy.assistant) == 1
    assert spy.assistant[0]["content"] == outgoing


async def test_second_round_sends_only_current(tmp_path):
    history = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "回答一"},
    ]
    manager, transports = make_manager(tmp_path, history)
    await drive_round(manager, transports, "当前消息 A")
    # 重建前 needs_reseed 仍为 True（同容器第二轮不重播种——直接驱动第二次 run_round）
    await drive_round(manager, transports, "当前消息 B")
    second = transports[0].written[1]["message"]
    assert second == "当前消息 B", "正常轮只发当前消息，不重复嵌历史"


async def test_rebuild_after_engine_death_reseeds_again(tmp_path):
    history = [
        {"role": "user", "content": "旧消息"},
        {"role": "assistant", "content": "旧回答"},
        {"role": "user", "content": "重建后新消息"},
    ]
    manager, transports = make_manager(tmp_path, history)
    await drive_round(manager, transports, "第一条")
    # 模拟容器死亡：EOF 使读取协程退出
    transports[0].emit_eof()
    await asyncio.sleep(0.05)
    await drive_round(manager, transports, "重建后第一条")
    assert len(transports) == 2, "引擎死亡后必须重建容器"
    outgoing = transports[1].written[0]["message"]
    assert outgoing.startswith("[历史对话回顾]"), "重建后的第一条消息必须重播种"
    assert "用户：旧消息" in outgoing


async def test_reseed_no_ghost_round(tmp_path):
    """重播种不单独发历史：一轮 prompt 只产生一条 assistant 落库。"""
    history = [
        {"role": "user", "content": "历史一"},
        {"role": "assistant", "content": "历史答一"},
        {"role": "user", "content": "新消息"},
    ]
    manager, transports = make_manager(tmp_path, history)
    _, spy = await drive_round(manager, transports, "新消息")
    assert len(transports[0].written) == 1, "重播种只发一条 prompt"
    assert len(spy.assistant) == 1


async def test_reseed_skipped_when_no_prior_history(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    await drive_round(manager, transports, "首条消息")
    outgoing = transports[0].written[0]["message"]
    assert outgoing == "首条消息", "无历史时不包回顾壳"


# ---------------------------------------------------------------------------
# 运行中补传附件（§6.6）
# ---------------------------------------------------------------------------


def _file(fid: int, name: str, path: str, size: int) -> dict:
    return {
        "id": fid, "original_name": name, "agent_path": path, "size_bytes": size,
    }


async def test_midchat_attachment_notice_appended_once(tmp_path):
    """运行中补传：新文件只在下一轮告知一次，且只列新增项。"""
    manager, transports = make_manager(tmp_path, [])
    f1 = _file(1, "a.md", "/task-files/a1", 2048)
    f2 = _file(2, "b.txt", "/task-files/b2", 3)

    await drive_round(manager, transports, "第一轮", task_files=[f1])
    first = transports[0].written[0]["message"]
    assert "[附件更新]" not in first, "容器播种时 manifest 已含 f1，无需告知"

    await drive_round(manager, transports, "看新附件", task_files=[f1, f2])
    second = transports[0].written[1]["message"]
    assert second.startswith("[附件更新]") is False
    assert "[附件更新]" in second
    assert "b.txt → /task-files/b2" in second
    assert "3 B" in second
    assert "a.md" not in second, "已播种文件不重复告知"

    await drive_round(manager, transports, "继续", task_files=[f1, f2])
    third = transports[0].written[2]["message"]
    assert third == "继续", "告知一次后不再重复"


async def test_attachment_notice_seeds_and_formats():
    """告知块格式正确，且告知后文件并入播种集合（幂等）。"""
    from types import SimpleNamespace

    manager, _ = make_manager(Path("."), [])
    engine = SimpleNamespace(seeded_file_ids=set(), task_id=1)
    files = [_file(7, "数据.csv", "/task-files/x7", 15360)]

    notice = manager._attachment_notice(engine, files)
    assert notice.startswith("[附件更新]")
    assert "数据.csv → /task-files/x7" in notice
    assert "15.0 KB" in notice
    assert engine.seeded_file_ids == {7}
    assert manager._attachment_notice(engine, files) == ""


async def test_reseed_message_places_notice_after_current(tmp_path):
    """重播种 + 补传附件同时发生：告知块位于 [当前消息] 之后。"""
    from types import SimpleNamespace

    history = [
        {"role": "user", "content": "旧问题"},
        {"role": "assistant", "content": "旧回答"},
    ]
    manager, _ = make_manager(tmp_path, history)
    engine = SimpleNamespace(needs_reseed=True, seeded_file_ids={1}, task_id=1)
    f2 = _file(2, "new.txt", "/task-files/n2", 5)

    message = await manager._build_outgoing_message(engine, 1, "新消息", [f2])
    assert message.index("[历史对话回顾]") < message.index("[当前消息]")
    assert message.index("[当前消息]") < message.index("[附件更新]")
    assert "new.txt → /task-files/n2" in message
    assert engine.seeded_file_ids == {1, 2}


# ---------------------------------------------------------------------------
# abort / 重建标记 / 令牌
# ---------------------------------------------------------------------------


async def test_request_abort_bypasses_and_writes_abort(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    engine = await manager.ensure_container(
        task_id=1,
        user_id=1,
        provider_config_id=None,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    engine.is_round_settled = False  # 模拟轮进行中
    await manager.request_abort(1)
    assert transports[0].aborted is True
    assert manager.has_active_round(1) is False or engine.is_round_settled is False


async def test_id_mismatch_rebuilds_on_next_ensure(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    engine = await manager.ensure_container(
        task_id=1,
        user_id=1,
        provider_config_id=None,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    transports[0].emit({"id": "req_bogus", "type": "response", "command": "x", "success": True})
    await asyncio.sleep(0.05)
    assert engine.needs_rebuild is True
    rebuilt = await manager.ensure_container(
        task_id=1,
        user_id=1,
        provider_config_id=None,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    assert rebuilt is not engine, "id 错配容器必须重建"


async def test_task_token_rotates_on_rebuild(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    await manager.ensure_container(
        task_id=1,
        user_id=1,
        provider_config_id=None,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    token_a = manager.get_task_token(1)
    transports[0].emit_eof()
    await asyncio.sleep(0.05)
    await manager.ensure_container(
        task_id=1,
        user_id=1,
        provider_config_id=None,
        stored_workdir="/workspaces/authorized",
        skill_snapshot=SNAPSHOT,
        task_files=[],
        expert_name="周报管家",
    )
    token_b = manager.get_task_token(1)
    assert token_a and token_b and token_a != token_b, "令牌随容器重建轮换"


async def test_error_round_emits_error_and_done(tmp_path):
    history = [{"role": "user", "content": "x"}]
    manager, transports = make_manager(tmp_path, history)

    async def failing_make_runtime(spec, workdir_host, extension_path):
        transport = FakePiTransport()
        transport.fail_after_prompt = True
        transports.append(transport)

        async def noop() -> None:
            return None

        return transport, noop

    manager._make_runtime = failing_make_runtime  # type: ignore[method-assign]
    events, spy = await drive_round(manager, transports, "触发错误")
    names = [name for name, _ in events]
    assert "error" in names
    assert names[-1] == "done"
    assert spy.assistant == [], "error 回复不落库"


# ---------------------------------------------------------------------------
# Provider 指纹（§7.7 双模式：改配置 → 下一轮重建 + 重播种）
# ---------------------------------------------------------------------------


async def test_provider_fingerprint_change_rebuilds_and_reseeds(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    await drive_round(manager, transports, "第一轮")

    # 当前生效配置变化（用户改了默认 Provider 的模型）
    manager._provider_snapshots.append(  # type: ignore[attr-defined]
        {"source": "user", "protocol": "openai",
         "base_url": "https://api.deepseek.com/v1", "model_id": "deepseek-chat",
         "api_key_encrypted": {"v": 1, "alg": "A256GCM", "kid": "primary",
                               "nonce": "x", "ciphertext": "y", "tag": "z"}}
    )
    history = [
        {"role": "user", "content": "历史一"},
        {"role": "assistant", "content": "历史答一"},
        {"role": "user", "content": "换源后第一条"},
    ]
    manager._history_fetcher = _fake_history_fetcher(history)  # type: ignore[method-assign]
    await drive_round(manager, transports, "换源后第一条")

    assert len(transports) == 2, "Provider 指纹变化必须重建容器"
    outgoing = transports[1].written[0]["message"]
    assert outgoing.startswith("[历史对话回顾]"), "重建后必须重播种"


async def test_same_provider_fingerprint_reuses_container(tmp_path):
    manager, transports = make_manager(tmp_path, [])
    await drive_round(manager, transports, "第一轮")
    await drive_round(manager, transports, "第二轮")
    assert len(transports) == 1, "指纹未变化不得重建"
