import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Paperclip, Plus, Stop, Warning, XCircle, Trash } from "@phosphor-icons/react";
import { request } from "../api/client.js";
import { SseClient } from "../api/sse.js";
import MessageList from "../components/MessageList.jsx";
import TaskContextPanel from "../components/TaskContextPanel.jsx";
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
    <aside className="task-sidebar rise" aria-label="任务列表">
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

/** P09 任务对话页：左侧任务列表 + 消息流式渲染 + 任务活跃期可上传附件。

  切换任务时中止在途 SSE 流并重置全部状态；refresh 带乱序守卫，
  过期响应直接丢弃；流结束后以服务端历史对账，对账失败保留乐观回复并提示。
  */
export default function TaskChatPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const taskId = id ? Number(id) : null;

  const [tasks, setTasks] = useState([]);
  const [task, setTask] = useState(null);
  const [isMissing, setIsMissing] = useState(false);
  const [loadError, setLoadError] = useState(null);
  const [notice, setNotice] = useState(null);

  const [streaming, setStreaming] = useState({
    active: false,
    text: "",
    thinking: "",
    toolCalls: [],
  });
  const [pendingUser, setPendingUser] = useState(null);
  // 对账失败时保留本次回复的乐观渲染，避免已显示内容凭空消失
  const [unpersistedReply, setUnpersistedReply] = useState(null);
  const [input, setInput] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [examples, setExamples] = useState([]);
  const sseRef = useRef(null);
  const activeTaskIdRef = useRef(taskId);
  const scrollRef = useRef(null);
  const fileInputRef = useRef(null);
  const composerRef = useRef(null);

  const refresh = useCallback(async () => {
    const forTaskId = taskId;
    if (forTaskId == null) {
      return false; // /tasks 列表页无任务 id，不发详情请求
    }
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
    setStreaming({ active: false, text: "", thinking: "", toolCalls: [] });
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

  // 切换任务 / 首次加载：直接定位到最新消息
  useEffect(() => {
    const container = scrollRef.current;
    if (container && task) {
      container.scrollTop = container.scrollHeight;
    }
    // 仅在任务标识变化时执行；task 内容更新由上方 near-bottom 逻辑接管
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 刻意只看 taskId
  }, [taskId, Boolean(task)]);

  // 发送后立即滚到底（用户主动发信必然关注回复）
  useEffect(() => {
    if (pendingUser && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [pendingUser]);

  // 空对话示例 chips：取专家公开详情的 task_examples（仅首条消息前展示）
  const hasUserMessage = (task?.messages ?? []).some((message) => message.role === "user");
  useEffect(() => {
    if (!task?.expert_id || hasUserMessage) {
      setExamples([]);
      return undefined;
    }
    let cancelled = false;
    request(`/api/experts/${task.expert_id}`)
      .then((payload) => {
        if (!cancelled) {
          setExamples(payload.data.task_examples ?? []);
        }
      })
      .catch(() => {
        // 示例是引导性内容，失败静默
      });
    return () => {
      cancelled = true;
    };
  }, [task?.expert_id, hasUserMessage]);

  async function handleSend(event) {
    event.preventDefault();
    const content = input.trim();
    const myTaskId = taskId;
    if (!content || !myTaskId || streaming.active) {
      return;
    }
    setInput("");
    if (composerRef.current) {
      composerRef.current.style.height = "auto"; // 发送后复位自增高
    }
    setNotice(null);
    setUnpersistedReply(null);
    setPendingUser(content);
    setStreaming({ active: true, text: "", thinking: "", toolCalls: [] });
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
        } else if (name === "thinking_delta") {
          setStreaming((current) => ({
            ...current,
            thinking: current.thinking + data.delta,
          }));
        } else if (name === "tool_event") {
          // start 建卡（running）；end 回填结果；update 忽略（v1 无增量展示）
          setStreaming((current) => {
            if (data.status === "start") {
              return {
                ...current,
                toolCalls: [
                  ...(current.toolCalls ?? []),
                  { name: data.name, args: data.args, status: "running", result: null },
                ],
              };
            }
            if (data.status === "end") {
              const calls = [...(current.toolCalls ?? [])];
              for (let i = calls.length - 1; i >= 0; i -= 1) {
                if (calls[i].name === data.name && calls[i].status === "running") {
                  calls[i] = {
                    ...calls[i],
                    status: data.isError ? "error" : "end",
                    result: data.result ?? "",
                  };
                  break;
                }
              }
              return { ...current, toolCalls: calls };
            }
            return current;
          });
        } else if (name === "message_saved") {
          lastText = data.content;
          setStreaming((current) => ({ ...current, text: data.content }));
        } else if (name === "queued") {
          setNotice("并发已满，任务进入排队；有容器空出后会自动开始本轮回复。");
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

  // -- 生命周期操作（PRD §4.5.4：中止/结束/删除；§4.5.6 约束） --------------

  async function handleAbort() {
    setNotice(null);
    try {
      await request(`/api/tasks/${taskId}/abort`, { method: "POST" });
      setNotice(null); // 轮将由 SSE done(aborted) 自然收尾
    } catch (cause) {
      setNotice(cause.message); // 409：当前没有可中止的 Agent 轮
    }
  }

  async function handleComplete() {
    if (!window.confirm("确定要结束该任务吗？结束后不可继续对话（历史保留）。")) {
      return;
    }
    sseRef.current?.abort(); // 有在途流先断开本地读取
    setNotice(null);
    try {
      await request(`/api/tasks/${taskId}/complete`, { method: "POST" });
      await refresh();
    } catch (cause) {
      setNotice(cause.message);
    }
  }

  async function handleDelete() {
    if (!window.confirm("确定要删除该任务吗？任务消息与上传文件将一并删除，且不可恢复。")) {
      return;
    }
    sseRef.current?.abort();
    try {
      await request(`/api/tasks/${taskId}`, { method: "DELETE" });
      navigate("/tasks");
    } catch (cause) {
      setNotice(cause.message);
    }
  }

  const messages = task?.messages ?? [];
  const isCompleted = task?.status === "completed";
  const isFailed = task?.status === "failed";
  // §6.6：任务活跃期（created/running/failed）可补传附件，终态拒绝；
  // 新文件由后端在下一轮对话中自动告知 Agent
  const canAttach =
    Boolean(task) && ["created", "running", "failed"].includes(task.status);
  const canSend = Boolean(task) && !isCompleted && !streaming.active;
  // 中止仅在有活动轮时可见；结束仅 running 可见；删除对已建任务始终可见
  const canAbort = Boolean(task) && task.status === "running" && streaming.active;
  const optimisticMessages = [
    ...(pendingUser ? [{ role: "user", content: pendingUser }] : []),
    ...(unpersistedReply ? [{ role: "assistant", content: unpersistedReply }] : []),
  ];
  // 调用记录 = 历史 tool 消息 + 流内进行中的调用（右侧面板）
  // toolCalls 用 ?? [] 兜底：HMR 快速刷新会保留旧形态的 streaming state
  const panelToolCalls = [
    ...messages
      .filter((message) => message.role === "tool")
      .map((message) => {
        const isError = message.content.startsWith("[tool_error] ");
        return {
          name: message.tool_name || "工具调用",
          status: isError ? "error" : "end",
          result: isError ? message.content.slice("[tool_error] ".length) : message.content,
          time: message.created_at,
        };
      }),
    ...(streaming.toolCalls ?? []).map((call) => ({ ...call, time: null })),
  ];

  return (
    <main className={`task-layout${taskId && task && !isMissing ? " has-context" : ""}`}>
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
            <header className="task-header rise" style={{ "--rise-index": 1 }}>
              <span className="task-avatar" aria-hidden="true">
                {(task.expert_name_snapshot || "专").slice(0, 1)}
              </span>
              <div className="task-header-info">
                <h2 className="task-header-title">{task.title}</h2>
                <div className="task-header-meta">
                  <span className="task-header-expert">{task.expert_name_snapshot}</span>
                  <span className="task-header-id">#{task.id}</span>
                  <span className="task-header-workdir" title="沙箱内以 /workspace 可见">
                    {task.workdir} · 沙箱内可见
                  </span>
                </div>
              </div>
              <span
                className={`runtime is-${streaming.active ? "running" : isCompleted ? "ended" : "idle"}`}
              >
                <span className="runtime-dot" aria-hidden="true" />
                {streaming.active ? "生成中" : isCompleted ? "已结束" : "空闲"}
              </span>
              <span className="task-header-actions">
                {canAbort && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={handleAbort}
                    title="中止当前一轮；未完成回复不保留"
                  >
                    <Stop size={13} weight="fill" aria-hidden="true" />
                    中止
                  </button>
                )}
                {task.status === "running" && !streaming.active && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={handleComplete}
                  >
                    <XCircle size={13} aria-hidden="true" />
                    结束对话
                  </button>
                )}
                <button
                  type="button"
                  className="btn btn-ghost btn-sm is-danger"
                  onClick={handleDelete}
                >
                  <Trash size={13} aria-hidden="true" />
                  删除
                </button>
              </span>
            </header>

            {isFailed && !notice && (
              <div className="task-notice" role="alert">
                <Warning size={15} aria-hidden="true" />
                <span>任务异常结束；重新发送一条消息即可重试（将重建容器并载入历史）。</span>
              </div>
            )}

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
              {!hasUserMessage && !streaming.active && messages.length === 0 && (
                <div className="empty-chat">
                  <h2>和「{task.expert_name_snapshot}」开始第一轮对话</h2>
                  <p>
                    描述你要完成的任务。发送后系统会装载专家人设、已启用 Skill
                    与工作目录，并冻结任务快照。
                  </p>
                  {examples.length > 0 && (
                    <div className="example-chips">
                      {examples.map((example) => (
                        <button
                          key={example}
                          type="button"
                          className="example-chip"
                          onClick={() => {
                            setInput(example);
                            composerRef.current?.focus();
                          }}
                        >
                          {example}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
              <MessageList
                messages={messages}
                pending={optimisticMessages}
                streamingText={streaming.text}
                isStreaming={streaming.active}
                streamingThinking={streaming.thinking ?? ""}
                streamingToolCalls={streaming.toolCalls ?? []}
                taskCreatedAt={task.created_at}
                skillsCount={task.skills?.length ?? 0}
              />
              {streaming.active && streaming.text === "" && streaming.thinking === "" && (
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

            <form className="composer rise" style={{ "--rise-index": 2 }} onSubmit={handleSend}>
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
                  title={
                    canAttach
                      ? "上传附件（下一轮对话中自动告知 Agent）"
                      : "任务已结束，不能上传附件"
                  }
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Paperclip size={15} aria-hidden="true" />
                  {isUploading ? "上传中…" : "附件"}
                </button>
                <textarea
                  ref={composerRef}
                  className="composer-input"
                  value={input}
                  rows={1}
                  maxLength={32000}
                  placeholder={isCompleted ? "任务已结束" : "输入消息，Enter 发送，Shift + Enter 换行"}
                  disabled={!canSend}
                  onChange={(event) => {
                    setInput(event.target.value);
                    const el = event.target;
                    el.style.height = "auto";
                    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
                  }}
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

      {taskId && !isMissing && task && (
        <TaskContextPanel
          skills={task.skills ?? []}
          toolCalls={panelToolCalls}
        />
      )}
    </main>
  );
}
