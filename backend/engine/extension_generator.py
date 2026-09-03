"""任务扩展生成器（Engineering Spec §7.4）。

为每个任务生成 Pi 扩展 `task.ts`，控制面写盘后只读挂载为容器内
`/extension/task.ts`。阶段 5 范围：

- faux Provider 注册（PI_PROVIDER=faux 时）：CLI 无内置 faux（实测
  `Unknown provider "faux"`），按官方 custom-provider 机制经扩展注册，
  自定义 streamSimple 回显对话上下文，供无 Key 联调与重播种验收
- MCP 工具注册循环：mcp_snapshot 能力上限快照写死（阶段 6 接 /internal/mcp/call，
  当前 v1 无 MCP 绑定，恒为空集）

扩展经 jiti 加载，`@earendil-works/pi-ai` 等导入由 alias/virtualModules
解析（与扩展文件路径无关），孤立挂载文件可正常 import。
模板占位用 __TOKEN__ 替换（TS 代码大括号多，str.format 转义不可维护）。
"""

from __future__ import annotations

import json
from pathlib import Path

# faux 回显上限：transcript 过长会经落库反馈膨胀，截断到尾部即可验证连续性
_FAUX_ECHO_MAX_CHARS = 2000

_EXTENSION_TEMPLATE = """\
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// 生成时写死：该任务专家已启用的 MCP 工具（mcp_snapshot 能力上限，§7.4）
const TOOLS: Array<{
  name: string; label: string; description: string;
  schema: Record<string, unknown>; serverId: number;
}> = __TOOLS_JSON__ as never;

const BACKEND = process.env.AGENTCRAFT_BACKEND_URL!;
const TASK_TOKEN = process.env.AGENTCRAFT_TASK_TOKEN!;
const TASK_ID = __TASK_ID__;

function contentToText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block: any) => {
      if (block?.type === "text") return block.text;
      if (block?.type === "thinking") return block.thinking;
      if (block?.type === "toolCall") return `${block.name}(${JSON.stringify(block.arguments)})`;
      if (block?.type === "image") return "[image]";
      return "";
    })
    .filter(Boolean)
    .join("\\n");
}

export default function (pi: ExtensionAPI) {
__FAUX_BLOCK__
  for (const t of TOOLS) {
    pi.registerTool({
      name: t.name,
      label: t.label,
      description: t.description,
      parameters: t.schema as never,
      execute: async (callId: string, args: Record<string, unknown>, signal: AbortSignal) => {
        const res = await fetch(`${BACKEND}/internal/mcp/call`, {
          method: "POST",
          headers: { "X-Task-Token": TASK_TOKEN, "Content-Type": "application/json" },
          body: JSON.stringify({
            task_id: TASK_ID, server_id: t.serverId, tool_name: t.name, args,
          }),
          signal,
        });
        if (!res.ok) {
          throw new Error(`MCP 调用失败: HTTP ${res.status}`);
        }
        const data = await res.json();
        const { content, is_error } = data.data as { content: string; is_error: boolean };
        if (is_error) {
          throw new Error(content);
        }
        return {
          content: [{ type: "text", text: content }],
          details: { server_id: t.serverId, tool_name: t.name },
        };
      },
    });
  }
}
"""

_FAUX_BLOCK_TEMPLATE = """\
  // faux Provider：CLI 无内置 faux，经扩展注册（PI_PROVIDER=faux 时由
  // AGENTCRAFT_PROVIDER=faux 启用）。回显对话上下文供无 Key 联调：
  // 回复 = 末条用户消息 + 完整上下文尾部，重播种后仍含历史事实，
  // 因此「回复中出现第 1 轮内容」即上下文连续性的确定性证据。
  pi.registerProvider("faux", {
    name: "Faux",
    baseUrl: "http://localhost:0",
    apiKey: "faux-no-key",
    api: "faux-echo",
    models: [{
      id: "faux-1",
      name: "Faux Echo",
      reasoning: false,
      input: ["text"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 128000,
      maxTokens: 16384,
    }],
    streamSimple(model, context, options) {
      const stream = createAssistantMessageEventStream();
      const CHUNK_DELAY = Number(process.env.AGENTCRAFT_FAUX_CHUNK_DELAY_MS || 0);
      void (async () => {
        const output = {
          role: "assistant",
          content: [],
          api: model.api,
          provider: model.provider,
          model: model.id,
          usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
                   cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
          stopReason: "pending",
          timestamp: Date.now(),
        };
        try {
          stream.push({ type: "start", partial: output });
          const messages = context.messages ?? [];
          const lastUser = [...messages].reverse().find((m) => m.role === "user");
          const transcript = messages
            .map((m) => `${m.role}:${contentToText(m.content)}`)
            .join("\\n");
          const tail = transcript.length > __ECHO_MAX__
            ? transcript.slice(-__ECHO_MAX__)
            : transcript;
          const text = `ECHO:${contentToText(lastUser?.content)}\\n---CTX---\\n${tail}`;
          output.content = [{ type: "text", text: "" }];
          const index = 0;
          stream.push({ type: "text_start", contentIndex: index, partial: output });
          const CHUNK = 24;
          for (let i = 0; i < text.length; i += CHUNK) {
            if (options?.signal?.aborted) throw new Error("Request was aborted");
            if (CHUNK_DELAY > 0) await new Promise((r) => setTimeout(r, CHUNK_DELAY));
            const delta = text.slice(i, i + CHUNK);
            output.content[index].text += delta;
            stream.push({ type: "text_delta", contentIndex: index, delta, partial: output });
          }
          stream.push({ type: "text_end", contentIndex: index, content: text, partial: output });
          output.stopReason = "stop";
          stream.push({ type: "done", reason: output.stopReason, message: output });
          stream.end(output);
        } catch (error) {
          output.stopReason = options?.signal?.aborted ? "aborted" : "error";
          output.errorMessage = error instanceof Error ? error.message : String(error);
          stream.push({ type: "error", reason: output.stopReason, error: output });
          stream.end(output);
        }
      })();
      return stream;
    },
  });"""

_NO_FAUX_BLOCK = "  // faux Provider 未启用（PI_PROVIDER != faux）"


class ExtensionGenerator:
    """生成任务扩展 task.ts（写盘到控制面数据目录，挂载前完成）。"""

    def __init__(self, extensions_root: Path) -> None:
        self.extensions_root = Path(extensions_root)

    def generate(self, task_id: int, mcp_tools: list[dict], provider: str) -> Path:
        """生成 task-<id>.ts，返回文件路径。mcp_tools 来自 mcp_snapshot。"""
        if provider == "faux":
            faux_block = _FAUX_BLOCK_TEMPLATE.replace(
                "__ECHO_MAX__", str(_FAUX_ECHO_MAX_CHARS)
            )
        else:
            faux_block = _NO_FAUX_BLOCK
        source = (
            _EXTENSION_TEMPLATE.replace(
                "__TOOLS_JSON__", json.dumps(mcp_tools, ensure_ascii=False)
            )
            .replace("__TASK_ID__", str(int(task_id)))
            .replace("__FAUX_BLOCK__", faux_block)
        )
        self.extensions_root.mkdir(parents=True, exist_ok=True)
        path = self.extensions_root / f"task-{int(task_id)}.ts"
        path.write_text(source, encoding="utf-8", newline="\n")
        return path
