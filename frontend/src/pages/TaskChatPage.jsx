import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Paperclip, Plus, Warning } from "@phosphor-icons/react";
import { request } from "../api/client.js";
import { SseClient } from "../api/sse.js";
import MessageList from "../components/MessageList.jsx";
import { formatBytes } from "../lib/format.js";
import { formatDateTime } from "../lib/datetime.js";

const STATUS_LABELS = {
  created: "待开始",
  running: "进行中",
  completed: "已结束",
  failed: "异常",
};

// 用户上翻回看时停止自动滚底的容差（像素）
const AUTOSCROLL_THRESHOLD_PX = 120;

function TaskSidebar({ tasks, activeId }) {
  return (
    <aside className="task-sidebar" aria-label="任务列表">
      <Link to="/tasks/new" className="btn btn-primary btn-sm task-sidebar-new">
        <Plus size={14} aria-hidden="true" /> 新任务
      </Link>
      <ul className="task-sidebar-list">
        {tasks.map((task) => (
          <li key={task.id}>
            <Link
              to={`/tasks/${task.id}`}
              className={"task-sidebar-item" + (task.id === activeId ? " is-active" : "")}
            >
              <span className="task-sidebar-title">{task.title}</span>
              <span className="task-sidebar-meta">
                <span className={`status-chip is-${task.status}`}>
                  {STATUS_LABELS[task.status] || task.status}
                </span>
                <time>{formatDateTime(task.created_at)}</time>
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </aside>
  );
}

/** P09 任务对话页：左侧任务列表 + 消息流式渲染 + 首条消息前可上传附件。

  切换任务时中止在途 SSE 流并重置全部状态；refresh 带乱序守卫，
  过期响应直接丢弃；流结束后以服务端历史对账，对账失败保留乐观回复并提示。
  */
export default function TaskChatPage() {
  const { id } = useParams();
  const taskId = id ? Number(id) : null;

  const [tasks, setTasks] = useState([]);
  const [task, setTask] = useState(null);
  const [isMissing, setIsMissing] = useState(false);
  const [loadError, setLoadError] = useState(null);
  const [notice, setNotice] = useState(null);

  const [streaming, setStreaming] = useState({ active: false, text: "" });
  const [pendingUser, setPendingUser] = useState(null);
  // 对账失败时保留本次回复的乐观渲染，避免已显示内容凭空消失
  const [unpersistedReply, setUnpersistedReply] = useState(null);
  const [input, setInput] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const sseRef = useRef(null);
  const activeTaskIdRef = useRef(taskId);
  const scrollRef = useRef(null);
  const fileInputRef = useRef(null);

  const refresh = useCallback(async () => {
    const forTaskId = taskId;
    try {
      const payload = await request(`/api/tasks/${forTaskId}`);
      if (activeTaskIdRef.current !== forTaskId) {
        return false; // 响应到达前已切换任务：丢弃过期数据
      }
      setTask(payload.data);
      setIsMissing(false);
      setLoadError(null);
      return true;
    } catch (cause) {
      if (activeTaskIdRef.current !== forTaskId) {
        return false;
      }
      if (cause.status === 404) {
        setIsMissing(true);
      } else {
        setLoadError(cause.message);
      }
      return false;
    }
  }, [taskId]);

  // 任务切换 / 卸载：中止在途 SSE 流，重置全部会话态
  useEffect(() => {
    activeTaskIdRef.current = taskId;
    sseRef.current?.abort();
    sseRef.current = null;
    setTask(null);
    setIsMissing(false);
    setLoadError(null);
    setNotice(null);
    setStreaming({ active: false, text: "" });
    setPendingUser(null);
    setUnpersistedReply(null);
    setInput("");
    refresh();
    return () => {
      sseRef.current?.abort();
      sseRef.current = null;
    };
  }, [taskId, refresh]);

  useEffect(() => {
    let cancelled = false;
    request("/api/tasks?page=1&size=50")
      .then((payload) => {
        if (!cancelled) {
          setTasks(payload.data);
        }
      })
      .catch(() => {
        // 侧栏是辅助导航，失败不打断主对话区
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, streaming.active]);

  // 流式渲染期间跟随滚动到底部；用户上翻回看时不打扰
  useEffect(() => {
    const container = scrollRef.current;
    if (!container) {
      return;
    }
    const distanceToBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    if (distanceToBottom < AUTOSCROLL_THRESHOLD_PX) {
      container.scrollTop = container.scrollHeight;
    }
  }, [task?.messages?.length, streaming]);

  async function handleSend(event) {
    event.preventDefault();
    const content = input.trim();
    const myTaskId = taskId;
    if (!content || !myTaskId || streaming.active) {
      return;
    }
    setInput("");
    setNotice(null);
    setUnpersistedReply(null);
    setPendingUser(content);
    setStreaming({ active: true, text: "" });
    const client = new SseClient(`/api/tasks/${myTaskId}/messages`);
    sseRef.current = client;
    let lastText = "";
    try {
      for await (const { event: name, data } of client.connect(content)) {
        if (activeTaskIdRef.current !== myTaskId) {
          break; // 已切换任务：不再更新共享状态
        }
        if (name === "text_delta") {
          lastText += data.delta;
          setStreaming((current) => ({ ...current, text: current.text + data.delta }));
        } else if (name === "message_saved") {
          lastText = data.content;
          setStreaming((current) => ({ ...current, text: data.content }));
        } else if (name === "error") {
          setNotice(data.recoverable ? `${data.message}（可重试）` : data.message);
        }
        // meta/done：done 后服务端随即收流，历史以 finally 中的服务端对账为准
      }
    } catch (cause) {
      const isAbort = cause?.name === "AbortError";
      if (!isAbort && activeTaskIdRef.current === myTaskId) {
        setNotice(cause.message);
      }
    } finally {
      sseRef.current = null;
      if (activeTaskIdRef.current !== myTaskId) {
        return; // 旧任务流的收尾：状态已随切换重置
      }
      const refreshed = await refresh();
      setStreaming({ active: false, text: "" });
      if (refreshed) {
        setPendingUser(null);
        setUnpersistedReply(null);
      } else if (lastText) {
        // 对账失败：保留乐观渲染的用户消息与回复，显式提示
        setUnpersistedReply(lastText);
        setNotice("历史刷新失败，最新回复可能未保存，请稍后重试");
      } else {
        setPendingUser(null);
        setNotice("历史刷新失败，请稍后重试");
      }
    }
  }

  async function handleFilesChosen(fileList) {
    const files = Array.from(fileList || []);
    if (files.length === 0 || !taskId) {
      return;
    }
    const formData = new FormData();
    for (const file of files) {
      formData.append("files", file);
    }
    setIsUploading(true);
    setNotice(null);
    try {
      await request(`/api/tasks/${taskId}/files`, { method: "POST", body: formData });
      await refresh();
    } catch (cause) {
      setNotice(cause.message);
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }

  const messages = task?.messages ?? [];
  const hasUserMessage = messages.some((message) => message.role === "user");
  const isCompleted = task?.status === "completed";
  const canAttach = Boolean(task) && task.status === "created" && !hasUserMessage;
  const canSend = Boolean(task) && !isCompleted && !streaming.active;
  const optimisticMessages = [
    ...(pendingUser ? [{ role: "user", content: pendingUser }] : []),
    ...(unpersistedReply ? [{ role: "assistant", content: unpersistedReply }] : []),
  ];

  return (
    <main className="task-layout">
      <TaskSidebar tasks={tasks} activeId={taskId} />

      <section className="task-main">
        {!taskId && (
          <div className="task-empty">
            <h2>选择或创建一个任务</h2>
            <p>从左侧选择历史任务，或召唤一位专家开始新的协作。</p>
            <Link to="/tasks/new" className="btn btn-primary">
              <Plus size={15} aria-hidden="true" /> 新任务
            </Link>
          </div>
        )}

        {taskId && isMissing && (
          <div className="task-empty">
            <h2>任务不存在</h2>
            <p>它可能已被删除，或不属于当前账号。</p>
            <Link to="/tasks" className="btn btn-ghost">
              <ArrowLeft size={14} aria-hidden="true" /> 返回任务列表
            </Link>
          </div>
        )}

        {taskId && !isMissing && loadError && !task && (
          <div className="task-empty">
            <h2>加载失败</h2>
            <p>{loadError}</p>
            <button type="button" className="btn btn-ghost" onClick={() => refresh()}>
              重试
            </button>
          </div>
        )}

        {taskId && !isMissing && task && (
          <>
            <header className="task-header">
              <div className="task-header-info">
                <h2 className="task-header-title">{task.title}</h2>
                <div className="task-header-meta">
                  <span className={`status-chip is-${task.status}`}>
                    {STATUS_LABELS[task.status] || task.status}
                  </span>
                  <span className="task-header-expert">{task.expert_name_snapshot}</span>
                  <span className="task-header-workdir">{task.workdir}</span>
                </div>
              </div>
              {isCompleted && <span className="task-header-ended">对话已结束</span>}
            </header>

            {notice && (
              <div className="task-notice" role="alert">
                <Warning size={15} aria-hidden="true" />
                <span>{notice}</span>
                <button
                  type="button"
                  className="task-notice-close"
                  aria-label="关闭提示"
                  onClick={() => setNotice(null)}
                >
                  ×
                </button>
              </div>
            )}

            <div className="message-scroll" ref={scrollRef}>
              <MessageList
                messages={messages}
                pending={optimisticMessages}
                streamingText={streaming.text}
                isStreaming={streaming.active}
              />
              {streaming.active && streaming.text === "" && (
                <p className="task-stream-hint">专家正在思考…</p>
              )}
            </div>

            {task.files.length > 0 && (
              <ul className="file-bar" aria-label="任务附件">
                {task.files.map((file) => (
                  <li className="file-chip" key={file.id}>
                    <Paperclip size={13} aria-hidden="true" />
                    <span className="file-chip-name">{file.original_name}</span>
                    <span className="file-chip-size">{formatBytes(file.size_bytes)}</span>
                  </li>
                ))}
              </ul>
            )}

            <form className="composer" onSubmit={handleSend}>
              <div className="composer-row">
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  hidden
                  onChange={(event) => handleFilesChosen(event.target.files)}
                />
                <button
                  type="button"
                  className="btn btn-ghost btn-sm composer-attach"
                  disabled={!canAttach || isUploading}
                  title={canAttach ? "上传附件（仅首条消息前）" : "首条消息发送后不可再上传"}
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Paperclip size={15} aria-hidden="true" />
                  {isUploading ? "上传中…" : "附件"}
                </button>
                <textarea
                  className="composer-input"
                  value={input}
                  rows={2}
                  maxLength={32000}
                  placeholder={
                    isCompleted ? "任务已结束" : "向专家描述你的需求，Enter 发送"
                  }
                  disabled={!canSend}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      handleSend(event);
                    }
                  }}
                />
                <button type="submit" className="btn btn-primary composer-send" disabled={!canSend}>
                  发送
                </button>
              </div>
            </form>
          </>
        )}
      </section>
    </main>
  );
}
