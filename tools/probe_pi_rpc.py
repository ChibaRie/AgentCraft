"""步骤 1 手工实验驱动：裸跑 pi --mode rpc（faux），录制真实帧序列。

产出：tests/fixtures/pi_frames/<name>.jsonl（协议测试的真实语料）。
用法：python tools/probe_pi_rpc.py <output-name> [timeout-seconds]
"""

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # agentcraft/
sys.path.insert(0, str(ROOT))

from backend.engine.extension_generator import ExtensionGenerator  # noqa: E402

FRAMES_DIR = ROOT / "tests" / "fixtures" / "pi_frames"

# Windows npm shim 是 .cmd，create_subprocess_exec 无法直接执行；直取包内 CLI 入口，
# 与容器内 argv（node + js 入口）形态一致。可用 PI_CLI_JS 覆盖。
DEFAULT_PI_CLI = (
    Path(os.environ.get("APPDATA", ""))
    / "npm/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
)
PI_CLI_JS = os.environ.get("PI_CLI_JS") or str(DEFAULT_PI_CLI)


async def main() -> None:  # noqa: C901 - 探针脚本，分支多为实验场景
    name = sys.argv[1] if len(sys.argv) > 1 else "faux_basic"
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    workdir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path.home() / ".pi-probe"
    scenario = sys.argv[4] if len(sys.argv) > 4 else "basic"

    # 工具选择子（V1 面冻结：仅 check_code_style，经 forwarder 回调
    # /internal/harness/*；Phase 8 随 V1 消亡。与 pi_engine_manager
    # ._V1_TRANSITION_TOOLS 一致；probe 保持轻量，不引入引擎模块依赖）
    extension = ExtensionGenerator(workdir / "extensions").generate(
        task_id=1, tools=[("check_code_style", "1")], provider="faux"
    )

    if scenario == "abort":
        # 发送长回复 prompt 后立即 abort，录制中止帧序（半截回复是否落库的语料）
        commands = [
            {"type": "get_state", "id": "req_1"},
            {"type": "prompt", "id": "req_2", "message": "ABORT-PROBE " + "长回复 " * 200},
        ]
    else:
        commands = [
            {"type": "get_state", "id": "req_1"},
            {"type": "prompt", "id": "req_2", "message": "你好，记住数字 42"},
            {"type": "prompt", "id": "req_3", "message": "我刚才说的数字是多少？只回答数字。"},
        ]

    proc = await asyncio.create_subprocess_exec(
        "node",
        PI_CLI_JS,
        "--mode",
        "rpc",
        "--no-session",
        "--system-prompt",
        "你是测试专家，回答极简。",
        "--approve",
        "--provider",
        "faux",
        "--model",
        "faux-1",
        "-e",
        str(extension),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
        env={
            **os.environ,
            "AGENTCRAFT_PROVIDER": "faux",
            # abort 场景需要可观察的流式时长
            "AGENTCRAFT_FAUX_CHUNK_DELAY_MS": "30" if scenario == "abort" else "0",
        },
    )

    frames: list[dict] = []
    stderr_text: list[str] = []
    settled = asyncio.Event()
    settled_count_target = 2  # 两轮 prompt → 两次 agent_settled

    async def reader() -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                frames.append({"_unparsed": line})
                continue
            frames.append(frame)
            print(json.dumps(frame, ensure_ascii=False)[:160], flush=True)
            if frame.get("type") == "agent_settled":
                count = len([f for f in frames if f.get("type") == "agent_settled"])
                if count >= settled_count_target:
                    settled.set()

    async def err_reader() -> None:
        assert proc.stderr is not None
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            stderr_text.append(raw.decode("utf-8", "replace").rstrip())

    async def feed() -> None:
        assert proc.stdin is not None
        for cmd in commands:
            proc.stdin.write((json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            await asyncio.sleep(0.3)
        if scenario == "abort":
            await asyncio.sleep(1.5)  # 让流式开始后再中止
            proc.stdin.write((json.dumps({"type": "abort"}) + "\n").encode("utf-8"))
            await proc.stdin.drain()

    reader_task = asyncio.create_task(reader())
    err_task = asyncio.create_task(err_reader())
    feed_task = asyncio.create_task(feed())

    try:
        await asyncio.wait_for(settled.wait(), timeout=timeout)
        print("[probe] two rounds settled", file=sys.stderr)
    except asyncio.TimeoutError:
        print(f"[probe] timeout after {timeout}s — collected {len(frames)} frames", file=sys.stderr)
    finally:
        feed_task.cancel()
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()

    reader_task.cancel()
    err_task.cancel()

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    out = FRAMES_DIR / f"{name}.jsonl"
    out.write_text(
        "\n".join(json.dumps(f, ensure_ascii=False) for f in frames) + "\n",
        encoding="utf-8",
    )
    print(f"\n[probe] {len(frames)} frames -> {out}")
    if stderr_text:
        print("[probe] stderr tail:", "\n".join(stderr_text[-5:]), file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
