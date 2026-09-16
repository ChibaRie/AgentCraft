import { useEffect, useReducer, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Plus, Stop, Trash, Warning, XCircle } from "@phosphor-icons/react";
import { newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { createTaskStream, fetchEvents } from "../api/v2/sse.js";
import { V2_TASKS } from "../api/v2/routes.js";
import MessageList from "../components/MessageList.jsx";
import TaskContextPanel from "../components/TaskContextPanel.jsx";
import { formatDateTime } from "../lib/datetime.js";
import { TASK_STATUS_LABELS } from "../lib/taskDisplay.js";
import {
  ABORT_REASON_LABELS,
  ACTIONABLE_STATUSES,
  AUTOSCROLL_THRESHOLD_PX,
  MESSAGE_MAX_CHARS,
  SIDEBAR_PAGE_SIZE,
  TERMINAL_STATUSES,
  describeSendError,
  initialTaskChatState,
  messagesPath,
  taskChatReducer,
} from "./taskChatReducer.js";
import { loadTaskBundle } from "./taskChatBundle.js";

/**
 * P09 任务对话页（Phase 8 T9 按 detached 执行模型全量重建；契约 = Sup §1.2/§1.3/
 * §4/§9.10.2/§9.10.8 + §10.3）。
 *
 * 数据编排（reducer 与装配层已外移：`taskChatReducer.js` / `taskChatBundle.js`，Phase 9 T7）：
 * - 初始五端点并行装配：任务快照 + messages?after=0 + files(input/output) + artifacts；
 * - 流消费 `createTaskStream({taskId, after: snapshot.event_sequence, ...})`（T8 冻结
 *   签名）：text_delta/thinking_delta 入瞬态流式缓冲；message_saved 无正文帧 → 经
 *   GET messages?after=<该帧前水位> 回补**替换** delta 缓冲；status_changed 状态迁移
 *   （终态丢弃未落库半截渲染）；done 轮终局刷新；queued/tool_event 对应 UI；
 * - **应用序单调约束（安全审查 I-6.2）**：sequence ≤ 已应用水位的持久帧/事件一律丢弃
 *   （防重连窗口 /events 补拉与实时帧并发到达乱序替换）；
 * - 重连对账：onEvents 逐帧幂等应用；快照水位领先时经 fetchEvents 分页续补（以已应用
 *   水位为游标循环拉取直至追平快照水位——T9 对账续补方案=分页续补，报告已申报）；
 * - 发消息：仅 ready 可发 → POST messages（幂等键）202 → 响应 event_sequence 直接
 *   校准水位（其下持久帧按单调约束丢弃，用户消息经响应 message.event_sequence 回补）；
 *   429 TASK_ROUND_BUSY/Retry-After 面向用户；
 * - abort/complete/delete 新语义：幂等键；running 分支 202 {round:{state:"cancelling"}}
 *   显示「终止中」直到 status_changed 终态帧收敛；404 信封引导退出任务页。
 */

// ---------------------------------------------------------------------------
// 视图子组件
// ---------------------------------------------------------------------------

/** 左侧任务列表（V2_TASKS 列表面；辅助导航，失败静默）。 */
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
              <span className="task-sidebar-title">{task.expert?.name || "未命名任务"}</span>
              <span className="task-sidebar-meta">
                <span className={`status-chip is-${task.status}`}>
                  {TASK_STATUS_LABELS[task.status] || task.status}
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

function NoticeBanner({ notice, onClear }) {
  return (
    <div className="task-notice" role="alert">
      <Warning size={15} aria-hidden="true" />
      <span>{notice}</span>
      <button type="button" className="task-notice-close" aria-label="关闭提示" onClick={onClear}>
        ×
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 页面
// ---------------------------------------------------------------------------

/** P09 任务对话页：detached 模型——SSE 实时流 + /events 对账兜底 + 幂等发送。 */
export default function TaskChatPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const taskId = id ? String(id) : null;

  const [state, dispatch] = useReducer(taskChatReducer, initialTaskChatState);
  const [sidebarTasks, setSidebarTasks] = useState([]);
  const [input, setInput] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const streamRef = useRef(null);
  const scrollRef = useRef(null);
  const composerRef = useRef(null);

  // 主装载 + 流建立（路由已按 id key 重挂载；reloadKey 供错误面重试）
  useEffect(() => {
    if (taskId === null) {
      return undefined;
    }
    let cancelled = false;
    loadTaskBundle(taskId)
      .then((bundle) => {
        if (cancelled) {
          return;
        }
        dispatch({ type: "LOAD_SUCCEEDED", ...bundle });
        streamRef.current = createTaskStream({
          taskId,
          after: Number.isInteger(bundle.task.event_sequence) ? bundle.task.event_sequence : 0,
          onFrame: (frame) =>
            dispatch({ type: "FRAME", id: frame.id, event: frame.event, data: frame.data }),
          onEvents: (events, snapshot) => dispatch({ type: "EVENTS_APPLIED", events, snapshot }),
          onError: (cause) => {
            // 非瞬时 4xx：404 信封引导退出任务页；403 面向用户；其余由 T8 退避重连
            if (cause?.status === 404) {
              streamRef.current?.close();
              dispatch({ type: "LOAD_MISSING" });
              return;
            }
            dispatch({
              type: "STREAM_ERROR",
              status: cause?.status,
              code: cause?.code,
              message: cause?.message,
            });
          },
        });
      })
      .catch((cause) => {
        if (cancelled) {
          return;
        }
        if (cause?.status === 404) {
          dispatch({ type: "LOAD_MISSING" });
        } else {
          dispatch({ type: "LOAD_FAILED", message: cause?.message });
        }
      });
    return () => {
      cancelled = true;
      streamRef.current?.close();
      streamRef.current = null;
    };
  }, [taskId, reloadKey]);

  // 左侧任务列表（辅助导航，失败静默）
  useEffect(() => {
    let cancelled = false;
    requestV2(`${V2_TASKS}?page=1&size=${SIDEBAR_PAGE_SIZE}`)
      .then((result) => {
        if (!cancelled) {
          setSidebarTasks(Array.isArray(result.data?.items) ? result.data.items : []);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSidebarTasks([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  // message_saved 回补：以「该帧前水位」为游标拉全量正文（无正文帧的权威回补面）
  useEffect(() => {
    if (state.phase !== "loaded" || state.pendingRefetchAfter === null || taskId === null) {
      return undefined;
    }
    const after = state.pendingRefetchAfter;
    let cancelled = false;
    requestV2(messagesPath(taskId, after))
      .then((result) => {
        if (!cancelled) {
          dispatch({
            type: "MESSAGES_MERGED",
            after,
            messages: Array.isArray(result.data) ? result.data : [],
          });
        }
      })
      .catch(() => {
        if (!cancelled) {
          dispatch({ type: "REFETCH_FAILED", after });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [state.phase, state.pendingRefetchAfter, taskId]);

  // 对账分页续补（断连窗口 >1000 持久事件的方案=分页拉齐：循环 after=已应用水位
  // 直至追平快照水位；单页拉空即放弃，接受下次重连收敛）
  useEffect(() => {
    if (
      state.phase !== "loaded" ||
      state.reconcileTarget === null ||
      taskId === null ||
      state.watermark >= state.reconcileTarget
    ) {
      return undefined;
    }
    let cancelled = false;
    fetchEvents(taskId, state.watermark)
      .then((page) => {
        if (cancelled) {
          return;
        }
        if (!Array.isArray(page.events) || page.events.length === 0) {
          // 单页拉空仍落后目标（理论不可达）：放弃本轮续补，接受下次重连收敛
          dispatch({ type: "RECONCILE_GIVE_UP" });
          return;
        }
        dispatch({
          type: "EVENTS_APPLIED",
          events: page.events,
          snapshot: { event_sequence: state.reconcileTarget },
        });
      })
      .catch(() => {
        if (!cancelled) {
          dispatch({ type: "RECONCILE_GIVE_UP" });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [state.phase, state.reconcileTarget, state.watermark, taskId]);

  // 轮终局刷新（done → 快照/messages 续拉/files/artifacts；部分失败容忍）
  useEffect(() => {
    if (!state.finalizing || taskId === null) {
      return undefined;
    }
    let cancelled = false;
    const base = `${V2_TASKS}/${encodeURIComponent(taskId)}`;
    const requests = [
      requestV2(base),
      requestV2(messagesPath(taskId, state.watermark)),
      requestV2(`${base}/files?direction=input`),
      requestV2(`${base}/artifacts`),
    ];
    Promise.allSettled(requests).then(([taskR, msgR, inR, artR]) => {
      if (cancelled) {
        return;
      }
      dispatch({
        type: "ROUND_FINALIZED",
        task: taskR.status === "fulfilled" ? taskR.value.data?.task ?? null : null,
        messages: msgR.status === "fulfilled" && Array.isArray(msgR.value.data) ? msgR.value.data : null,
        inputFiles:
          inR.status === "fulfilled" && Array.isArray(inR.value.data) ? inR.value.data : null,
        artifacts:
          artR.status === "fulfilled" && Array.isArray(artR.value.data) ? artR.value.data : null,
      });
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- finalizing 翻转时以当轮渲染水位为准
  }, [state.finalizing, taskId]);

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
  }, [state.messages.length, state.streaming, state.pendingUser]);

  // 切换任务 / 首次加载：直接定位到最新消息
  useEffect(() => {
    const container = scrollRef.current;
    if (container && state.task) {
      container.scrollTop = container.scrollHeight;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 刻意只看任务标识
  }, [taskId, Boolean(state.task)]);

  // -- 用户操作 ---------------------------------------------------------------

  async function handleSend(event) {
    event.preventDefault();
    const content = input.trim();
    if (!content || taskId === null || state.task?.status !== "ready") {
      return;
    }
    setInput("");
    if (composerRef.current) {
      composerRef.current.style.height = "auto";
    }
    try {
      const result = await requestV2(`${V2_TASKS}/${encodeURIComponent(taskId)}/messages`, {
        method: "POST",
        body: { content },
        idempotencyKey: newIdempotencyKey(),
      });
      const data = result.data ?? {};
      const messageSeq = Number(data.message?.event_sequence);
      const watermarkSeq = Number(data.event_sequence);
      dispatch({
        type: "SEND_ACCEPTED",
        content,
        messageSeq: Number.isInteger(messageSeq) ? messageSeq : null,
        watermarkSeq: Number.isInteger(watermarkSeq) ? watermarkSeq : null,
      });
    } catch (cause) {
      dispatch({ type: "NOTICE_SET", notice: describeSendError(cause) });
    }
  }

  async function runTerminalIntent(endpoint) {
    try {
      const result = await requestV2(`${V2_TASKS}/${encodeURIComponent(taskId)}/${endpoint}`, {
        method: "POST",
        idempotencyKey: newIdempotencyKey(),
      });
      const data = result.data ?? {};
      dispatch({ type: "TERMINAL_ACCEPTED", task: data.task ?? null, cancelling: Boolean(data.round) });
    } catch (cause) {
      dispatch({ type: "NOTICE_SET", notice: cause?.message ?? "操作失败，请稍后重试" });
    }
  }

  async function handleDelete() {
    if (!window.confirm("确定要删除该任务吗？任务消息与上传文件将一并删除，且不可恢复。")) {
      return;
    }
    streamRef.current?.close();
    try {
      await requestV2(`${V2_TASKS}/${encodeURIComponent(taskId)}`, {
        method: "DELETE",
        idempotencyKey: newIdempotencyKey(),
      });
      navigate("/tasks");
    } catch (cause) {
      dispatch({ type: "NOTICE_SET", notice: cause?.message ?? "删除失败，请稍后重试" });
    }
  }

  // -- 派生视图 ----------------------------------------------------------------

  const task = state.task;
  const isTerminal = task !== null && TERMINAL_STATUSES.has(task.status);
  const canSend = state.phase === "loaded" && task?.status === "ready";
  const canAbort =
    state.phase === "loaded" && !state.roundCancelling && ACTIONABLE_STATUSES.includes(task?.status);
  const canComplete = canAbort;
  const runtimeLabel = state.roundCancelling
    ? "终止中…"
    : state.streaming.active
      ? "生成中"
      : isTerminal
        ? "已结束"
        : "空闲";
  const runtimeTone = state.roundCancelling || state.streaming.active ? "running" : isTerminal ? "ended" : "idle";
  const viewMessages = state.messages.map((message) => ({
    ...message,
    role: message.author,
  }));
  const hasStreamContent =
    state.streaming.text !== "" ||
    state.streaming.thinking !== "" ||
    state.streaming.toolCalls.length > 0;
  const showStreamingBubble = state.streaming.active && hasStreamContent;
  const showThinkingHint =
    task?.status === "running" && !state.roundCancelling && !showStreamingBubble;

  return (
    <main className={`task-layout${taskId && task ? " has-context" : ""}`}>
      <TaskSidebar tasks={sidebarTasks} activeId={taskId} />

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

        {taskId && state.phase === "missing" && (
          <div className="task-empty">
            <h2>任务不存在</h2>
            <p>它可能已被删除，或不属于当前账号。</p>
            <Link to="/tasks" className="btn btn-ghost">
              <ArrowLeft size={14} aria-hidden="true" /> 返回任务列表
            </Link>
          </div>
        )}

        {taskId && state.phase === "error" && (
          <div className="task-empty">
            <h2>加载失败</h2>
            <p>{state.loadError}</p>
            <button type="button" className="btn btn-ghost" onClick={() => setReloadKey((n) => n + 1)}>
              重试
            </button>
          </div>
        )}

        {taskId && state.phase === "loading" && (
          <div className="task-empty">
            <h2>正在装配任务…</h2>
          </div>
        )}

        {taskId && state.phase === "loaded" && task && (
          <>
            <header className="task-header rise" style={{ "--rise-index": 1 }}>
              <span className="task-avatar" aria-hidden="true">
                {(task.expert?.name || "专").slice(0, 1)}
              </span>
              <div className="task-header-info">
                <h2 className="task-header-title">{task.expert?.name || "任务"}</h2>
                <div className="task-header-meta">
                  <span className={`status-chip is-${task.status}`}>
                    {TASK_STATUS_LABELS[task.status] || task.status}
                  </span>
                  <span className="task-header-expert">
                    {task.provider
                      ? `${task.provider.display_name ?? ""}${task.provider.model ? ` · ${task.provider.model}` : ""}`
                      : "Provider 快照不可见"}
                  </span>
                  <span className="task-header-id">#{task.id}</span>
                  <span className="task-header-workdir">{formatDateTime(task.created_at)} 创建</span>
                </div>
              </div>
              <span className={`runtime is-${runtimeTone}`}>
                <span className="runtime-dot" aria-hidden="true" />
                {runtimeLabel}
              </span>
              <span className="task-header-actions">
                {canAbort && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => runTerminalIntent("abort")}
                    title="中止当前一轮或取消排队；未完成回复不保留"
                  >
                    <Stop size={13} weight="fill" aria-hidden="true" />
                    中止
                  </button>
                )}
                {canComplete && (
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => {
                      if (window.confirm("确定要结束该任务吗？结束后不可继续对话（历史保留）。")) {
                        runTerminalIntent("complete");
                      }
                    }}
                  >
                    <XCircle size={13} aria-hidden="true" />
                    结束对话
                  </button>
                )}
                <button type="button" className="btn btn-ghost btn-sm is-danger" onClick={handleDelete}>
                  <Trash size={13} aria-hidden="true" />
                  删除
                </button>
              </span>
            </header>

            {task.status === "failed" && !state.notice && (
              <div className="task-notice" role="alert">
                <Warning size={15} aria-hidden="true" />
                <span>任务异常结束，历史消息保留。</span>
              </div>
            )}
            {task.status === "aborted" && task.abort_reason && !state.notice && (
              <div className="task-notice" role="alert">
                <Warning size={15} aria-hidden="true" />
                <span>任务已中止（{ABORT_REASON_LABELS[task.abort_reason] ?? task.abort_reason}）。</span>
              </div>
            )}
            {state.notice && <NoticeBanner notice={state.notice} onClear={() => dispatch({ type: "NOTICE_CLEARED" })} />}

            <div className="message-scroll" ref={scrollRef}>
              <MessageList
                messages={viewMessages}
                pending={state.pendingUser ? [{ role: "user", content: state.pendingUser.content }] : []}
                streamingText={state.streaming.text}
                isStreaming={showStreamingBubble}
                streamingThinking={state.streaming.thinking}
                streamingToolCalls={state.streaming.toolCalls}
              />
              {showThinkingHint && <p className="task-stream-hint">专家正在思考…</p>}
            </div>

            <form className="composer rise" style={{ "--rise-index": 2 }} onSubmit={handleSend}>
              <div className="composer-row">
                <textarea
                  ref={composerRef}
                  className="composer-input"
                  value={input}
                  rows={1}
                  maxLength={MESSAGE_MAX_CHARS}
                  placeholder={
                    canSend
                      ? "输入消息，Enter 发送，Shift + Enter 换行"
                      : isTerminal
                        ? "任务已结束"
                        : "当前轮次执行中，等待结束后可继续"
                  }
                  disabled={!canSend}
                  onChange={(event) => {
                    setInput(event.target.value);
                    const el = event.target;
                    el.style.height = "auto";
                    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
                  }}
                  onKeyDown={(keyEvent) => {
                    if (keyEvent.key === "Enter" && !keyEvent.shiftKey) {
                      keyEvent.preventDefault();
                      handleSend(keyEvent);
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

      {taskId && state.phase === "loaded" && task && (
        <TaskContextPanel
          taskId={taskId}
          task={task}
          inputFiles={state.inputFiles}
          artifacts={state.artifacts}
        />
      )}
    </main>
  );
}
