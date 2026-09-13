"""任务扩展生成器（阶段 5：平台工具常量表 × 选择子驱动注册）。

为每个任务生成 Pi 扩展 `task.ts`，控制面写盘后只读挂载为容器内
`/extension/task.ts`。用户 MCP 工具面已下线（Phase 5 裁决 D8）——注册块
所需的 name/label/description/parameters 全部来自平台工具常量描述符表
（backend.engine.platform_tools），调用方只传 (tool_id, version) 选择子，
DB 原文永不进入模板。阶段 5 范围：

- faux Provider 注册（PI_PROVIDER=faux 时）：CLI 无内置 faux（实测
  `Unknown provider "faux"`），按官方 custom-provider 机制经扩展注册，
  自定义 streamSimple 回显对话上下文，供无 Key 联调与重播种验收
- 平台工具注册循环：harness 类工具经 /internal 端点回调（凭 X-Task-Token，
  回调时点第二校验在 /internal）；container 类工具 Phase 5 不注册
  （Phase 6 随卷模型交付）

扩展经 jiti 加载，`@earendil-works/pi-ai` 等导入由 alias/virtualModules
解析（与扩展文件路径无关），孤立挂载文件可正常 import。
模板占位用 __TOKEN__ 替换（TS 代码大括号多，str.format 转义不可维护）；
注入顺序（faux/task_id 先、TOOLS JSON 最后）加占位 token 断言构成
模板安全化（S2 §5）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from backend.engine.platform_tools import PLATFORM_TOOLS

# faux 回显上限：transcript 过长会经落库反馈膨胀，截断到尾部即可验证连续性
_FAUX_ECHO_MAX_CHARS = 2000

# 模板安全化（S2 §5）：产物残留 __TOKEN__ 形态 → 拒绝写盘
_TEMPLATE_TOKEN_RE = re.compile(r"__[A-Z_]+__")

_EXTENSION_TEMPLATE = """\
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// 生成时写死：该任务允许使用的平台工具（常量描述符表 × 选择子，§4.6）。
// label/description/schema 全部为控制面常量——数据原文不进模板。
const TOOLS: Array<{
  name: string; version: string; label: string; description: string;
  schema: Record<string, unknown>; callbackPath: string;
}> = __TOOLS_JSON__ as never;

const BACKEND = process.env.AGENTCRAFT_BACKEND_URL!;
const TASK_TOKEN = process.env.AGENTCRAFT_TASK_TOKEN!;
const TASK_ID = __TASK_ID__;
const OPENAI_BASE_URL = process.env.OPENAI_BASE_URL!;
const PROVIDER_MODEL = process.env.AGENTCRAFT_PROVIDER_MODEL!;

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
        const res = await fetch(`${BACKEND}${t.callbackPath}`, {
          method: "POST",
          headers: { "X-Task-Token": TASK_TOKEN, "Content-Type": "application/json" },
          body: JSON.stringify({ task_id: TASK_ID, ...args }),
          signal,
        });
        if (!res.ok) {
          let message = `平台工具调用失败: HTTP ${res.status}`;
          try {
            const err = await res.json();
            if (err?.error?.message) message = err.error.message;
          } catch {}
          throw new Error(message);
        }
        const data = await res.json();
        return {
          content: [{ type: "text", text: JSON.stringify(data.data) }],
          details: { tool: t.name, version: t.version },
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

_NO_FAUX_BLOCK = """  // OpenAI 兼容上游走 chat/completions 协议：经扩展注册覆盖内置 openai
  // provider（内置实现使用新版 Responses API，DeepSeek/Ollama 等第三方
  // 端点普遍未实现，直连会 404）。Base URL 指向 provider-proxy，真实
  // Key 由 proxy 按任务令牌侧解密注入，容器内只有任务令牌（§7.7）。
  pi.registerProvider("openai", {
    name: "AgentCraft Provider",
    baseUrl: OPENAI_BASE_URL,
    apiKey: TASK_TOKEN,
    api: "openai-completions",
    models: [
      {
        id: PROVIDER_MODEL,
        name: PROVIDER_MODEL,
        reasoning: false,
        // 声明图像输入：Pi read 工具按 model.input 决定是否剥离图像块
        // （getNonVisionImageNote）；V1 无能力目录，统一声明支持，
        // 纯文本模型读图将收到 provider 侧报错（可观察错误，不崩溃）。
        // V2 BYOK 目录化（阶段3）改为按模型能力声明。
        input: __MODEL_INPUT__,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 128000,
        maxTokens: 16384,
      },
    ],
  });"""


class ExtensionGenerator:
    """生成任务扩展 task.ts（写盘到控制面数据目录，挂载前完成）。"""

    def __init__(self, extensions_root: Path) -> None:
        self.extensions_root = Path(extensions_root)

    def generate(
        self,
        task_id: int,
        tools: Sequence[tuple[str, str]],
        provider: str,
        *,
        model_input: Sequence[str] = ("text", "image"),
    ) -> Path:
        """生成 task-<id>.ts。tools 为 (tool_id, version) 选择子：
        ① 未知组合 → ValueError（生成时点白名单，调用方负责 enabled 校验——
           引擎层无 DB；回调时点第二校验在 /internal，Phase 6 任务创建为第一时点）；
        ② kind="harness" 按选择子注册；kind="container" Phase 5 不注册（无实现，
           Phase 6 随卷模型交付）；
        ③ 模板安全化（S2 §5）：faux/task_id 先注入、TOOLS JSON 最后注入，且注入前
           断言序列化结果不含 __[A-Z_]+__ 形态 token（命中 ValueError 拒生成）。
        """
        registered: list[dict] = []
        for tool_id, version in tools:
            tool = PLATFORM_TOOLS.get((tool_id, version))
            if tool is None:
                raise ValueError(f"未知平台工具: {tool_id}@{version}")
            if tool.kind != "harness":
                continue  # Phase 6：container 工具随卷模型/回调实现交付
            registered.append(
                {
                    "name": tool.tool_id,
                    "version": tool.version,
                    "label": tool.label,
                    "description": tool.description,
                    "schema": tool.parameters,
                    "callbackPath": tool.callback_path,
                }
            )
        if provider == "faux":
            # faux 回显模型无图像输入——保持 ["text"] 硬编码，不吃 model_input
            faux_block = _FAUX_BLOCK_TEMPLATE.replace("__ECHO_MAX__", str(_FAUX_ECHO_MAX_CHARS))
        else:
            faux_block = _NO_FAUX_BLOCK.replace("__MODEL_INPUT__", json.dumps(list(model_input)))
        source = (
            _EXTENSION_TEMPLATE.replace("__FAUX_BLOCK__", faux_block)
            .replace("__TASK_ID__", str(int(task_id)))
            .replace("__MODEL_INPUT__", json.dumps(list(model_input)))
            .replace("__TOOLS_JSON__", json.dumps(registered, ensure_ascii=False))
            # TOOLS JSON 最后注入：不再被后续替换扫描（S2 §5 缺陷 A 闭环）
        )
        if _TEMPLATE_TOKEN_RE.search(source):
            raise ValueError("生成产物含未替换/非法模板占位 token，拒绝写盘")
        self.extensions_root.mkdir(parents=True, exist_ok=True)
        path = self.extensions_root / f"task-{int(task_id)}.ts"
        path.write_text(source, encoding="utf-8", newline="\n")
        return path
