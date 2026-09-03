"""真实 MCP stdio 链路探针（阶段 6 mcp-sandbox 验收辅助）。

前置：docker 可用，且已构建沙箱镜像
    docker compose -f docker/docker-compose.yml --profile mcp-sandbox build mcp-sandbox

执行：python probe_mcp_stdio.py
流程：McpClient.stdio → `docker run --rm -i --network agentcraft-internal
agentcraft-mcp-sandbox sh -c "mcp-server-filesystem /workspace"` →
initialize → tools/list → tools/call(list_directory) 全真实执行。
"""

import asyncio
import json
import subprocess
import sys

sys.path.insert(0, ".")

from backend.config import get_settings  # noqa: E402
from backend.engine.mcp_client import McpClient  # noqa: E402

NETWORK = "agentcraft-internal"
COMMAND = "mcp-server-filesystem /workspace"


def ensure_network() -> None:
    result = subprocess.run(
        ["docker", "network", "inspect", NETWORK], capture_output=True
    )
    if result.returncode != 0:
        subprocess.run(
            ["docker", "network", "create", "--internal", NETWORK], check=True
        )
        print(f"[probe] created internal network {NETWORK}")
    else:
        print(f"[probe] network {NETWORK} exists")


async def main() -> None:
    ensure_network()
    settings = get_settings()
    async with McpClient.stdio(COMMAND, {}, settings) as client:
        tools = await client.discover()
        print(f"[probe] tools/list: {len(tools)} tools")
        for tool in tools:
            print(f"  - {tool['name']}: {tool['description'][:60]}")
        result = await client.call("list_directory", {"path": "/workspace"})
        print(f"[probe] tools/call list_directory → is_error={result['is_error']}")
        print(f"[probe] content: {result['content']!r}")
        assert result["is_error"] is False, "list_directory 不应失败"
        print("[probe] OK：stdio 沙箱链路全通")


if __name__ == "__main__":
    asyncio.run(main())
    print(json.dumps({"probe": "mcp_stdio", "status": "ok"}))
