import { formatDateTime } from "../lib/datetime.js";

const ROLE_LABELS = { user: "你", assistant: "专家" };

function Bubble({ role, content, createdAt, isStreaming = false }) {
  return (
    <div className={`message is-${role}`}>
      <div className="message-meta">
        <span className="message-role">{ROLE_LABELS[role] || role}</span>
        {createdAt && <time className="message-time">{formatDateTime(createdAt)}</time>}
      </div>
      <div className="message-bubble">
        {content}
        {isStreaming && <span className="stream-caret" aria-hidden="true" />}
      </div>
    </div>
  );
}

/**
 * 消息列表（P09）：按时间顺序渲染持久化历史；
 * 流式回复以 isStreaming 气泡附加在末尾，text_delta 逐字追加。
 */
export default function MessageList({ messages, pending, streamingText, isStreaming }) {
  return (
    <div className="message-list">
      {messages.map((message) => (
        <Bubble
          key={message.id}
          role={message.role}
          content={message.content}
          createdAt={message.created_at}
        />
      ))}
      {(pending || []).map((message, index) => (
        <Bubble
          key={`pending-${index}`}
          role={message.role}
          content={message.content}
        />
      ))}
      {isStreaming && (
        <Bubble role="assistant" content={streamingText} isStreaming />
      )}
    </div>
  );
}
