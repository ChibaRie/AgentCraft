import { formatDateTime } from "../lib/datetime.js";
import { renderMarkdown } from "../lib/markdown.js";

const ROLE_LABELS = { user: "你", assistant: "专家" };

/** 危险 HTML 已在 renderMarkdown 内转义，此处可安全使用。 */
function Markdown({ text }) {
  // eslint-disable-next-line react/no-danger -- renderMarkdown 先转义后替换，XSS 安全
  return <div className="md-body" dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }} />;
}

/** 工具调用行：只展示工具名 + 状态（结果详情见右侧「调用记录」面板）。 */
function ToolCard({ name, status }) {
  const isError = status === "error";
  const stateLabel =
    status === "running" ? "执行中…" : isError ? "调用失败" : status === "end" ? "已完成" : "";
  return (
    <div className={`tool-call is-compact${isError ? " is-error" : ""}`}>
      <span className="tool-call-name">{name}</span>
      {stateLabel && <span className="tool-call-state">{stateLabel}</span>}
    </div>
  );
}

/** 思考行：thinking_delta 流式展示（不落库，仅过程呈现）。 */
function ThinkingLine({ text }) {
  if (!text) {
    return null;
  }
  return (
    <p className="thinking-line">
      <span className="thinking-mark" aria-hidden="true">
        ◌
      </span>
      {text}
    </p>
  );
}

function Bubble({
  role,
  content,
  createdAt,
  isStreaming = false,
  thinking = "",
  toolCalls = [],
}) {
  const isAssistant = role === "assistant";
  return (
    <div className={`message is-${role}`}>
      <div className="message-meta">
        <span className="message-role">{ROLE_LABELS[role] || role}</span>
        {createdAt && <time className="message-time">{formatDateTime(createdAt)}</time>}
        {isAssistant && (
          <span className="message-state">{isStreaming ? "生成中…" : "已保存"}</span>
        )}
      </div>
      <div className="message-bubble">
        {isAssistant && <ThinkingLine text={thinking} />}
        {toolCalls.map((call, index) => (
          <ToolCard key={`call-${index}-${call.name}`} {...call} />
        ))}
        {isStreaming ? (
          <>
            {content}
            <span className="stream-caret" aria-hidden="true" />
          </>
        ) : role === "user" ? (
          <Markdown text={content} />
        ) : (
          <Markdown text={content} />
        )}
      </div>
    </div>
  );
}

/** 历史消息：tool 消息渲染为独立工具卡片（错误前缀转状态徽标）。 */
function HistoryMessage({ message }) {
  if (message.role === "tool") {
    const isError = message.content.startsWith("[tool_error] ");
    return (
      <div className="message is-tool">
        <ToolCard name={message.tool_name || "工具调用"} status={isError ? "error" : "end"} />
      </div>
    );
  }
  return (
    <Bubble role={message.role} content={message.content} createdAt={message.created_at} />
  );
}

/** 任务创建系统便签（快照加载信息）。 */
function SystemNote({ skillsCount, mcpCount, createdAt }) {
  if (!createdAt) {
    return null;
  }
  return (
    <p className="system-note">
      任务已创建 · 已加载 {skillsCount} 个 Skill、{mcpCount} 个 MCP 工具 · 快照已冻结（
      {formatDateTime(createdAt)}）
    </p>
  );
}

/**
 * 消息列表（P09）：历史消息 + 系统便签 + 流式气泡（思考行/工具卡片/光标）。
 * assistant 已完成气泡与用户气泡走 Markdown 渲染；流式中文本保持纯文本
 * 避免半截 Markdown 反复重排。
 */
export default function MessageList({
  messages,
  pending,
  streamingText,
  isStreaming,
  streamingThinking = "",
  streamingToolCalls = [],
  taskCreatedAt = null,
  skillsCount = 0,
  mcpCount = 0,
}) {
  return (
    <div className="message-list">
      <SystemNote
        skillsCount={skillsCount}
        mcpCount={mcpCount}
        createdAt={taskCreatedAt}
      />
      {messages.map((message) => (
        <HistoryMessage key={message.id} message={message} />
      ))}
      {(pending || []).map((message, index) => (
        <Bubble key={`pending-${index}`} role={message.role} content={message.content} />
      ))}
      {isStreaming && (
        <Bubble
          role="assistant"
          content={streamingText}
          isStreaming
          thinking={streamingThinking}
          toolCalls={streamingToolCalls}
        />
      )}
    </div>
  );
}
