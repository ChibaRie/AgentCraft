"""任务扩展生成器（阶段 5：平台工具常量表 × 选择子驱动注册）。

为每个任务生成 Pi 扩展 `task.ts`，控制面写盘后只读挂载为容器内
`/extension/task.ts`。用户 MCP 工具面已下线（Phase 5 裁决 D8）——注册块
所需的 name/label/description/parameters 全部来自平台工具常量描述符表
（backend.engine.platform_tools），调用方只传 (tool_id, version) 选择子，
DB 原文永不进入模板。阶段 5 范围：

- faux Provider 注册（PI_PROVIDER=faux 时）：CLI 无内置 faux（实测
  `Unknown provider "faux"`），按官方 custom-provider 机制经扩展注册，
  自定义 streamSimple 回显对话上下文，供无 Key 联调与重播种验收
- 平台工具注册循环：callback_path 非空即注册（Phase 6 D2 全回调——harness 与
  container 统一走 /internal 端点回调，凭 X-Task-Token，回调时点第二校验在
  /internal；Phase 5 的「container 不注册」中间态随 D2 裁决演进消亡）

扩展经 jiti 加载，`@earendil-works/pi-ai` 等导入由 alias/virtualModules
解析（与扩展文件路径无关），孤立挂载文件可正常 import。
模板占位用 __TOKEN__ 替换（TS 代码大括号多，str.format 转义不可维护）；
注入顺序（faux/task_id/TOOLS JSON 先、model_input 载荷最后）加模板区
占位 token 断言（数据区豁免）构成模板安全化（S2 §5，Phase 6 D5 方案 a）。
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

// Phase 10 M6：用户挂载的 MCP 工具（快照冻结；全部经 /internal/mcp/call
// 回调治理链执行）。工具名统一加 user_ 前缀防与平台工具撞名。
const USER_MCP_TOOLS: Array<{
  name: string; label: string; description: string;
  schema: Record<string, unknown>; serverId: string; toolName: string;
}> = __USER_MCP_TOOLS_JSON__ as never;

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
  for (const t of USER_MCP_TOOLS) {
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
            task_id: TASK_ID,
            server_id: t.serverId,
            tool_name: t.toolName,
            arguments: args,
          }),
          signal,
        });
        if (!res.ok) {
          let message = `MCP 工具调用失败: HTTP ${res.status}`;
          try {
            const err = await res.json();
            if (err?.error?.message) message = err.error.message;
          } catch {}
          throw new Error(message);
        }
        const data = await res.json();
        return {
          content: [{ type: "text", text: JSON.stringify(data.data) }],
          details: { tool: t.name, serverId: t.serverId, toolName: t.toolName },
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


def _user_mcp_registration(user_mcp_tools: Sequence[dict]) -> list[dict]:
    """用户 MCP 工具快照 → task.ts 注册块（Phase 10 M6）。

    统一加 user_ 前缀防与平台工具撞名；形态异常条目跳过（外部缓存防御）。
    """
    registered: list[dict] = []
    for item in user_mcp_tools:
        if not isinstance(item, dict):
            continue
        tool_name = item.get("tool_name")
        server_id = item.get("server_id")
        if not isinstance(tool_name, str) or not tool_name:
            continue
        if not isinstance(server_id, str) or not server_id:
            continue
        schema = item.get("schema")
        registered.append(
            {
                "name": f"user_{tool_name}",
                "label": str(item.get("label") or tool_name),
                "description": str(item.get("description") or ""),
                "schema": schema if isinstance(schema, dict) else {"type": "object"},
                "serverId": server_id,
                "toolName": tool_name,
            }
        )
    return registered


class ExtensionGenerator:
    """生成任务扩展 task.ts（写盘到控制面数据目录，挂载前完成）。"""

    def __init__(self, extensions_root: Path) -> None:
        self.extensions_root = Path(extensions_root)

    def generate(
        self,
        task_id: int | str,
        tools: Sequence[tuple[str, str]],
        provider: str,
        *,
        model_input: Sequence[str] = ("text", "image"),
        user_mcp_tools: Sequence[dict] = (),
    ) -> Path:
        """生成 task-<task_id>.ts。tools 为 (tool_id, version) 选择子：
        ① 未知组合 → ValueError（生成时点白名单，调用方负责 enabled 校验——
           引擎层无 DB；回调时点第二校验在 /internal，Phase 6 任务创建为第一时点）；
        ② callback_path 非空即注册（Phase 6 D2 全回调：harness/container 统一
           路径——四容器工具经 /internal/tools/* 回调，check_code_style 经
           /internal/harness/*；callback_path 为空的描述符不注册）；
        ③ 模板安全化（S2 §5，Phase 6 D5 方案 a）：faux/task_id/TOOLS JSON/用户
           MCP 描述符先注入，终检断言模板区零 token（唯一豁免是待填充的
           __MODEL_INPUT__ 占位符，命中 ValueError 拒生成）；model_input 载荷
           最后注入且此后无任何 replace/扫描——数据区豁免 token 断言（载荷中
           token 字样原样出产物）。
        ④ Phase 10 M6：user_mcp_tools 为快照冻结的用户 MCP 工具描述符
           （{name,label,description,schema,server_id,tool_name}）——name 统一
           加 user_ 前缀防撞名；schema 形态/长度在服务层收口，此处只做形态防御。

        task_id 类型（Phase 6 T6a，D17 申报例外）：int → 数字字面量（V1 路径
        byte-identical）；str（V2 UUID）→ 带引号的 TS 字符串字面量（json.dumps
        转义），文件名 ``task-<task_id>.ts`` 原样取串。
        """
        registered_user = _user_mcp_registration(user_mcp_tools)

        registered: list[dict] = []
        for tool_id, version in tools:
            tool = PLATFORM_TOOLS.get((tool_id, version))
            if tool is None:
                raise ValueError(f"未知平台工具: {tool_id}@{version}")
            if not tool.callback_path:
                continue  # D2 全回调：callback_path 非空即注册（无回调面即无实现）
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
        if isinstance(task_id, int):
            task_id_text = str(int(task_id))  # V1 int 路径：数字字面量 + task-<int>.ts
            task_id_literal = task_id_text
        else:
            task_id_text = str(task_id)  # V2 UUID 路径：串原样；字面量带引号安全转义
            task_id_literal = json.dumps(task_id_text)
        if provider == "faux":
            # faux 回显模型无图像输入——保持 ["text"] 硬编码，不吃 model_input
            faux_block = _FAUX_BLOCK_TEMPLATE.replace("__ECHO_MAX__", str(_FAUX_ECHO_MAX_CHARS))
        else:
            # __MODEL_INPUT__ 占位符保留到终检之后填充（D5 方案 a）：载荷不得
            # 随 faux_block 提前进入 source，否则被后续 replace 链扫描/改写
            faux_block = _NO_FAUX_BLOCK
        source = (
            _EXTENSION_TEMPLATE.replace("__FAUX_BLOCK__", faux_block)
            .replace("__TASK_ID__", task_id_literal)
            .replace("__TOOLS_JSON__", json.dumps(registered, ensure_ascii=False))
            .replace(
                "__USER_MCP_TOOLS_JSON__",
                json.dumps(registered_user, ensure_ascii=False),
            )
        )
        # 终检（S2 §5 缺陷 A 闭环，Phase 6 D5 方案 a）：位于 TOOLS JSON 注入之后、
        # model_input 注入之前——此时载荷尚未进入 source，断言作用于模板区。
        # 唯一合法残留是非 faux 路径待填充的 __MODEL_INPUT__ 占位符；其余任何
        # __[A-Z_]+__ 形态（含 TOOLS 载荷携带者）→ ValueError 拒绝写盘。
        expected_pending = [] if provider == "faux" else ["__MODEL_INPUT__"]
        if _TEMPLATE_TOKEN_RE.findall(source) != expected_pending:
            raise ValueError("生成产物含未替换/非法模板占位 token，拒绝写盘")
        # model_input 载荷最后注入：此后不再有任何 replace/扫描（数据区豁免）
        source = source.replace("__MODEL_INPUT__", json.dumps(list(model_input)))
        self.extensions_root.mkdir(parents=True, exist_ok=True)
        path = self.extensions_root / f"task-{task_id_text}.ts"
        path.write_text(source, encoding="utf-8", newline="\n")
        return path
