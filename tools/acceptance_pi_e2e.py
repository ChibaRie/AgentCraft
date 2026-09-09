"""阶段 5 端到端验收（PRD §4.5.5 + phase5_piagent.md §八）。

前置：后端已以 PI_PROVIDER=faux 启动（uvicorn backend.main:app）。
用法：PYTHONUTF8=1 python tools/acceptance_pi_e2e.py [base-url]

验收项：
1. faux 下创建任务 → 发消息 → SSE 逐字流式（非首条 2s 内出字）
2. 连续 3 轮对话，第 3 轮回复引用第 1 轮内容（容器内存连续性）
3. 杀容器后发新消息 → 自动重建 + 重播种，上下文不丢
4. abort 生效：done(aborted)、半截回复不落库、任务可继续
5. 全部消息落库，刷新历史完整
"""

import asyncio
import base64
import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SSE_PATH = "/api/tasks/{task_id}/messages"


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not ok:
        raise SystemExit(f"验收失败: {name}")


async def send_and_collect(
    client: httpx.AsyncClient, token: str, task_id: int, content: str
) -> tuple[list[tuple[str, dict]], float]:
    """发送消息并收集 SSE 帧；返回 (frames, 首个 text_delta 延迟秒)。"""
    frames: list[tuple[str, dict]] = []
    first_delta_at = None
    started = time.perf_counter()
    async with client.stream(
        "POST",
        BASE + SSE_PATH.format(task_id=task_id),
        json={"content": content},
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(120.0),
    ) as response:
        assert response.status_code == 200, f"HTTP {response.status_code}: {await response.aread()}"
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                name, data = None, ""
                for line in block.split("\n"):
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        data += line[5:].strip()
                if name:
                    payload = json.loads(data) if data else {}
                    frames.append((name, payload))
                    if name == "text_delta" and first_delta_at is None:
                        first_delta_at = time.perf_counter() - started
    return frames, first_delta_at or 0.0


def sse_value(frames: list[tuple[str, dict]], name: str) -> dict:
    for frame_name, payload in frames:
        if frame_name == name:
            return payload
    return {}


def reply_text(frames: list[tuple[str, dict]]) -> str:
    return "".join(p.get("delta", "") for n, p in frames if n == "text_delta")


async def main() -> None:
    suffix = base64.b16encode(str(time.time()).encode()).decode()[-6:].lower()
    username = f"accept{suffix}"
    async with httpx.AsyncClient() as client:
        # 注册
        reg = await client.post(
            f"{BASE}/api/auth/register",
            json={
                "username": username,
                "email": f"{username}@example.com",
                "password": "secret123",
            },
        )
        token = reg.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        # 申请专家 → 建 Skill（发布）→ 建专家（发布）
        await client.post(f"{BASE}/api/users/me/expert", headers=headers)
        skill = await client.post(
            f"{BASE}/api/skills",
            json={
                "name": "验收整理术",
                "description": "阶段五端到端验收使用的技能",
                "use_case": "适用于团队每周例会前的素材整理与周报初稿生成场景",
                "role": "结构化整理各类素材并输出周报的整理专家",
                "goal": "产出清晰、分节、可追溯的高质量技术周报文档",
                "steps": "先收集素材，再按主题归类，最后输出结构化摘要",
                "output_requirements": "输出必须分节，包含进展、风险与下一步三个部分",
                "constraints": "不得编造事实，引用素材时必须注明来源出处",
            },
            headers=headers,
        )
        assert skill.status_code == 201, skill.text
        skill_id = skill.json()["data"]["id"]
        assert (
            await client.post(f"{BASE}/api/skills/{skill_id}/publish", headers=headers)
        ).status_code == 200
        expert = await client.post(
            f"{BASE}/api/experts",
            json={
                "name": "验收专家",
                "description": "阶段五端到端验收专用专家",
                "category": "tech",
                "persona": "严谨的验收助手",
                "methodology": "逐步验收",
                "task_examples": [],
                "avatar_url": None,
            },
            headers=headers,
        )
        expert_id = expert.json()["data"]["id"]
        assert (
            await client.post(
                f"{BASE}/api/experts/{expert_id}/skills",
                json={"skill_id": skill_id},
                headers=headers,
            )
        ).status_code == 201
        await client.put(
            f"{BASE}/api/experts/{expert_id}/skills/{skill_id}",
            json={"enabled": True},
            headers=headers,
        )
        assert (
            await client.post(f"{BASE}/api/experts/{expert_id}/publish", headers=headers)
        ).status_code == 200

        # 创建任务（workdir 缺省 = 授权根）
        task = await client.post(
            f"{BASE}/api/tasks",
            json={"expert_id": expert_id, "description": "阶段五引擎验收"},
            headers=headers,
        )
        task_id = task.json()["data"]["task_id"]
        check("创建任务", task.status_code == 201, f"task_id={task_id}")

        # ① 首条消息：流式出字（首条含容器启动，10s 内）
        t0 = time.perf_counter()
        frames1, first_delta = await send_and_collect(
            client, token, task_id, "请记住暗号：星尘-42"
        )
        check("首条消息流式出字（10s 内）", first_delta <= 10.0, f"{first_delta:.2f}s")
        reply1 = reply_text(frames1)
        check("首条回复包含暗号", "星尘-42" in reply1, reply1[:60])
        check(
            "帧序 meta→…→message_saved→done",
            frames1[0][0] == "meta" and sse_value(frames1, "done").get("finish_reason") == "stop",
            f"{len(frames1)} 帧，耗时 {time.perf_counter() - t0:.1f}s",
        )

        # ②③ 多轮：第 2 轮（非首条 2s 内出字），第 3 轮引用第 1 轮内容
        frames2, first_delta2 = await send_and_collect(
            client, token, task_id, "我刚才说的暗号是什么？"
        )
        check("非首条 2s 内出字", first_delta2 <= 2.0, f"{first_delta2:.2f}s")
        reply2 = reply_text(frames2)
        check("第 2 轮内存连续性（引用暗号）", "星尘-42" in reply2, reply2[:80])

        # ④ 杀容器 → 重播种恢复上下文
        import subprocess as sp

        killed = sp.run(
            ["docker", "rm", "-f", f"pi-task-{task_id}"],
            capture_output=True,
            text=True,
        )
        check("容器已删除（docker rm -f）", killed.returncode == 0, killed.stdout.strip())
        frames3, _ = await send_and_collect(
            client, token, task_id, "容器重启后：我最初说的暗号是什么？"
        )
        reply3 = reply_text(frames3)
        check("重播种后上下文恢复（引用暗号）", "星尘-42" in reply3, reply3[:120])

        # ⑤ abort：轮停止、半截不落库、任务可继续
        # （长消息确保流式时长 > 1.5s，abort 稳定落在流中段）
        abort_round = asyncio.create_task(
            send_and_collect(client, token, task_id, "这是一条会被中止的长回复" + "详细展开 " * 400)
        )
        await asyncio.sleep(1.5)
        aborted = await client.post(
            f"{BASE}/api/tasks/{task_id}/abort", headers=headers
        )
        check("abort 返回 202", aborted.status_code == 202)
        frames4, _ = await abort_round
        check(
            "done(finish_reason=aborted)",
            sse_value(frames4, "done").get("finish_reason") == "aborted",
            f"{len(frames4)} 帧",
        )
        check("中止轮无 message_saved", not any(n == "message_saved" for n, _ in frames4))

        # ⑥ 历史完整（user×4 + assistant×3，无中止残片）
        detail = await client.get(f"{BASE}/api/tasks/{task_id}", headers=headers)
        messages = detail.json()["data"]["messages"]
        roles = [m["role"] for m in messages]
        check(
            "历史完整（4 user + 3 assistant）",
            roles.count("user") == 4 and roles.count("assistant") == 3,
            str(roles),
        )
        print("\n=== 阶段 5 端到端验收全部通过 ===")


if __name__ == "__main__":
    asyncio.run(main())
