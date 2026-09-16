import { useEffect, useReducer, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ArrowLeft, Plus, Stop, Trash, Warning, XCircle } from "@phosphor-icons/react";
import { V2ApiError, newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { createTaskStream, fetchEvents } from "../api/v2/sse.js";
import { V2_TASKS } from "../api/v2/routes.js";
import MessageList from "../components/MessageList.jsx";
import TaskContextPanel from "../components/TaskContextPanel.jsx";
import { formatDateTime } from "../lib/datetime.js";
import { TASK_STATUS_LABELS } from "../lib/taskDisplay.js";

/**
 * P09 任务对话页（Phase 8 T9 按 detached 执行模型全量重建；契约 = Sup §1.2/§1.3/
 * §4/§9.10.2/§9.10.8 + §10.3）。
 *
 * 数据编排（组件内纯 reducer `taskChatReducer`，便于测试）：
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

const TERMINAL_STATUSES = new Set(["completed", "failed", "aborted", "deleted"]);
// abort/complete 的受理状态集（Sup §1.2:29/30——uploading 用 DELETE 终态化，终态 409）
const ACTIONABLE_STATUSES = ["queued", "running", "ready"];
const MESSAGE_MAX_CHARS = 65536;
const MESSAGES_PAGE_LIMIT = 200;
const SIDEBAR_PAGE_SIZE = 50;
// 用户上翻回看时停止自动滚底的容差（像素）
const AUTOSCROLL_THRESHOLD_PX = 120;

const ABORT_REASON_LABELS = {
  user_cancel: "用户取消",
  round_failed: "执行失败",
  upload_expired: "上传超时",
  provider_key_revoked: "Provider 凭据已撤销",
  tool_revoked: "工具已停用",
  admin_suspended: "账号被停用",
};

const EMPTY_STREAMING = Object.freeze({ active: false, text: "", thinking: "", toolCalls: [] });

function messagesPath(taskId, after) {
  return `${V2_TASKS}/${encodeURIComponent(taskId)}/messages?after=${Math.max(0, after)}&limit=${MESSAGES_PAGE_LIMIT}`;
}

// ---------------------------------------------------------------------------
// 纯 reducer（应用序单调约束在此收口；导出供测试直测）
// ---------------------------------------------------------------------------

export const initialTaskChatState = {
  phase: "loading", // loading | loaded | missing | error
  loadError: null,
  task: null,
  messages: [],
  watermark: 0, // 已应用事件水位（I-6.2 单调闸）
  inputFiles: [],
  artifacts: [],
  pendingRefetchAfter: null, // messages 回补游标（最小待拉 event_sequence - 1）
  pendingReplaceSeq: null, // 待替换 delta 缓冲的 message_saved sequence
  reconcileTarget: null, // 对账目标水位（快照 watermark 领先时触发分页续补）
  finalizing: false, // done → 轮终局刷新在途
  pendingUser: null, // {content, messageSeq} 发送乐观位
  roundCancelling: false, // abort/complete 202 受理 → status_changed 终态帧前显示「终止中」
  streaming: EMPTY_STREAMING,
  notice: null,
};

/** 历史消息合并：按 id 去重（服务端权威覆盖）、按 event_sequence 升序。 */
function mergeMessages(existing, incoming) {
  const byId = new Map();
  for (const message of existing) {
    byId.set(message.id, message);
  }
  for (const message of incoming ?? []) {
    if (message && message.id) {
      byId.set(message.id, message);
    }
  }
  return [...byId.values()].sort((a, b) => a.event_sequence - b.event_sequence);
}

/** 消息页满 → 以页尾 event_sequence 为游标续拉下一页（limit 上限 200 的分页续补）。 */
function continuationCursor(messages) {
  if (!Array.isArray(messages) || messages.length < MESSAGES_PAGE_LIMIT) {
    return null;
  }
  return messages.reduce((max, message) => Math.max(max, message.event_sequence ?? 0), 0);
}

function withStreaming(state, patch) {
  return { ...state, streaming: { ...state.streaming, ...patch } };
}

/** 终态迁移：丢弃未落库半截渲染 + 解除「终止中」（Sup §1.3:50）。 */
function withTerminalClear(state, status) {
  if (!TERMINAL_STATUSES.has(status)) {
    return status === "running" ? { ...state, roundCancelling: false } : state;
  }
  return { ...state, streaming: EMPTY_STREAMING, roundCancelling: false };
}

function applyStatusChanged(state, data) {
  const status = data?.status;
  if (typeof status !== "string" || !state.task) {
    return state;
  }
  const task = {
    ...state.task,
    status,
    abort_reason: data.abort_reason ?? state.task.abort_reason,
  };
  return withTerminalClear({ ...state, task }, status);
}

function applyMessageSaved(state, seq) {
  const after = Math.max(0, seq - 1);
  return {
    ...state,
    pendingRefetchAfter: Math.min(state.pendingRefetchAfter ?? after, after),
    pendingReplaceSeq: seq,
  };
}

function applyToolEvent(state, data) {
  const calls = state.streaming.toolCalls;
  if (data?.status === "start") {
    return withStreaming(state, {
      active: true,
      toolCalls: [
        ...calls,
        { name: data.name ?? "工具调用", args: data.args ?? null, status: "running", result: null },
      ],
    });
  }
  if (data?.status === "end") {
    const next = [...calls];
    for (let i = next.length - 1; i >= 0; i -= 1) {
      if (next[i].name === data.name && next[i].status === "running") {
        next[i] = { ...next[i], status: data.isError ? "error" : "end", result: data.result ?? "" };
        break;
      }
    }
    return withStreaming(state, { active: true, toolCalls: next });
  }
  return state; // update：忽略（增量无独立展示，V1 同型）
}

/** 单帧应用（帧类型词表 = Sup §1.3:38-46）。未知类型静默忽略（前向兼容）。 */
function applyFrameEvent(state, event, data, seq) {
  switch (event) {
    case "text_delta":
      return withStreaming(state, { active: true, text: state.streaming.text + String(data?.delta ?? "") });
    case "thinking_delta":
      return withStreaming(state, { active: true, thinking: state.streaming.thinking + String(data?.delta ?? "") });
    case "tool_event":
      return applyToolEvent(state, data);
    case "message_saved":
      return applyMessageSaved(state, seq);
    case "status_changed":
      return applyStatusChanged(state, data);
    case "done":
      // 轮终局：流式气泡收场 + 触发终局刷新（终局收敛经终局刷新兜底）
      return { ...state, streaming: EMPTY_STREAMING, finalizing: true };
    case "queued":
      return { ...state, notice: "任务已进入排队，等待运行槽位分配。" };
    case "error":
      return {
        ...state,
        notice: data?.recoverable ? `${data.message ?? "执行出错"}（可重试）` : data?.message ?? "执行出错",
      };
    default:
      return state; // meta 等：瞬态帧无消费规则（快照已由初始装配提供）
  }
}

/**
 * 帧应用入口（I-6.2）：持久帧（id 非 null）sequence ≤ 已应用水位整体丢弃；
 * 通过后应用并以该 sequence 推进水位；瞬态帧不动水位。
 */
function applySequencedFrame(state, id, event, data) {
  if (id !== null) {
    if (id <= state.watermark) {
      return state;
    }
    return { ...applyFrameEvent(state, event, data, id), watermark: id };
  }
  return applyFrameEvent(state, event, data, null);
}

/** 快照字段收敛（abort/complete/delete 响应的 task 视图 → 本地快照）。 */
function pickSnapshotFields(task) {
  return {
    status: task.status,
    abort_reason: task.abort_reason,
    active_round: task.active_round ?? null,
    initial_round: task.initial_round ?? null,
    counts: task.counts ?? null,
  };
}

function mergeBackfilledMessages(state, incoming) {
  const usable = (incoming ?? []).filter((message) => message && message.id);
  if (usable.length === 0) {
    return state;
  }
  let next = { ...state, messages: mergeMessages(state.messages, usable) };
  // 发送乐观位收敛：落库用户消息（响应 message.event_sequence）到位即撤
  if (
    next.pendingUser &&
    usable.some((m) => m.author === "user" && m.event_sequence === next.pendingUser.messageSeq)
  ) {
    next = { ...next, pendingUser: null };
  }
  // assistant 落库 → 以权威正文替换对应 delta 缓冲（仅限帧宣告的那条消息）
  if (usable.some((m) => m.author === "assistant" && m.event_sequence === state.pendingReplaceSeq)) {
    next = {
      ...next,
      streaming: { ...next.streaming, text: "", thinking: "" },
      pendingReplaceSeq: null,
    };
  }
  // tool 落库 → 已结束的流内工具卡由历史卡片接管（防双渲染）
  if (usable.some((m) => m.author === "tool")) {
    next = {
      ...next,
      streaming: {
        ...next.streaming,
        toolCalls: next.streaming.toolCalls.filter((call) => call.status === "running"),
      },
    };
  }
  return next;
}

export function taskChatReducer(state, action) {
  switch (action.type) {
    case "LOAD_SUCCEEDED": {
      const task = action.task;
      const messages = mergeMessages([], action.messages);
      return {
        ...state,
        phase: "loaded",
        task,
        messages,
        inputFiles: action.inputFiles ?? [],
        artifacts: action.artifacts ?? [],
        watermark:
          Number.isInteger(task?.event_sequence) && task.event_sequence > 0
            ? task.event_sequence
            : state.watermark,
        pendingRefetchAfter: continuationCursor(messages) ?? state.pendingRefetchAfter,
      };
    }
    case "LOAD_MISSING":
      return { ...state, phase: "missing" };
    case "LOAD_FAILED":
      return { ...state, phase: "error", loadError: action.message ?? "加载失败，请稍后重试" };
    case "FRAME": {
      const id = Number.isInteger(action.id) && action.id > 0 ? action.id : null;
      return applySequencedFrame(state, id, action.event, action.data);
    }
    case "EVENTS_APPLIED": {
      let next = state;
      for (const item of action.events ?? []) {
        const seq = Number(item?.sequence);
        const id = Number.isInteger(seq) && seq > 0 ? seq : null;
        next = applySequencedFrame(next, id, String(item?.type ?? "message"), item);
      }
      const snapshot = action.snapshot;
      if (snapshot && next.task && typeof snapshot.status === "string") {
        next = {
          ...next,
          task: {
            ...next.task,
            status: snapshot.status,
            abort_reason: snapshot.abort_reason ?? next.task.abort_reason,
          },
        };
      }
      const target = Number(snapshot?.event_sequence);
      if (Number.isInteger(target) && target > next.watermark) {
        next = { ...next, reconcileTarget: Math.max(next.reconcileTarget ?? 0, target) };
      }
      if (next.reconcileTarget !== null && next.watermark >= next.reconcileTarget) {
        next = { ...next, reconcileTarget: null };
      }
      return next;
    }
    case "RECONCILE_GIVE_UP":
      // 单页拉空仍落后目标（理论不可达）：接受本轮不追平，下次重连收敛
      return { ...state, reconcileTarget: null };
    case "MESSAGES_MERGED": {
      const next = mergeBackfilledMessages(state, action.messages);
      const continuation = continuationCursor(action.messages);
      if (continuation !== null) {
        // 页满续拉优先于游标清除（分页未到尾页）
        return { ...next, pendingRefetchAfter: Math.max(continuation, next.pendingRefetchAfter ?? 0) };
      }
      if (state.pendingRefetchAfter === action.after) {
        return { ...next, pendingRefetchAfter: null };
      }
      return next;
    }
    case "REFETCH_FAILED":
      // 回补失败：清除游标防死循环；done 终局刷新与下一次 message_saved 帧兜底
      return state.pendingRefetchAfter === action.after
        ? { ...state, pendingRefetchAfter: null }
        : state;
    case "SEND_ACCEPTED": {
      const { content, messageSeq, watermarkSeq } = action;
      let next = { ...state, notice: null, pendingUser: { content, messageSeq } };
      if (Number.isInteger(watermarkSeq) && watermarkSeq > state.watermark) {
        next = { ...next, watermark: watermarkSeq };
      }
      if (Number.isInteger(messageSeq)) {
        const after = Math.max(0, messageSeq - 1);
        next = { ...next, pendingRefetchAfter: Math.min(next.pendingRefetchAfter ?? after, after) };
      }
      if (next.task) {
        // 发送事务 ready→queued（Sup §1.2:25）；对应帧 ≤ 校准水位，由本处直改承接
        next = { ...next, task: { ...next.task, status: "queued" } };
      }
      return next;
    }
    case "TERMINAL_ACCEPTED": {
      let next = { ...state, roundCancelling: Boolean(action.cancelling) };
      if (action.task && next.task) {
        next = { ...next, task: { ...next.task, ...pickSnapshotFields(action.task) } };
      }
      if (next.task && TERMINAL_STATUSES.has(next.task.status)) {
        next = { ...next, streaming: EMPTY_STREAMING, roundCancelling: false };
      }
      return next;
    }
    case "ROUND_FINALIZED": {
      let next = { ...state, finalizing: false, streaming: EMPTY_STREAMING };
      if (action.task && next.task) {
        next = { ...next, task: { ...next.task, ...pickSnapshotFields(action.task) } };
      }
      if (action.inputFiles) {
        next = { ...next, inputFiles: action.inputFiles };
      }
      if (action.artifacts) {
        next = { ...next, artifacts: action.artifacts };
      }
      if (action.messages) {
        next = { ...next, messages: mergeMessages(next.messages, action.messages) };
        if (
          next.pendingUser &&
          (next.pendingUser.messageSeq === null ||
            action.messages.some(
              (m) => m.author === "user" && m.event_sequence === next.pendingUser.messageSeq
            ))
        ) {
          // 落库行到位即撤乐观位；202 响应缺 message.event_sequence（异常形态）时终局兜底
          next = { ...next, pendingUser: null };
        }
      }
      return next;
    }
    case "STREAM_ERROR":
      if (action.status === 403) {
        return { ...state, notice: action.message ?? "无权访问该任务" };
      }
      return { ...state, notice: "事件流连接中断，正在自动重连…" };
    case "NOTICE_SET":
      return { ...state, notice: action.notice };
    case "NOTICE_CLEARED":
      return { ...state, notice: null };
    case "RESET":
      return initialTaskChatState;
    default:
      return state;
  }
}

// ---------------------------------------------------------------------------
// 数据装载（五端点并行）
// ---------------------------------------------------------------------------

/** 初始装配：快照 + messages?after=0 + files(input/output) + artifacts（Sup §1.2/§4）。 */
async function loadTaskBundle(taskId) {
  const base = `${V2_TASKS}/${encodeURIComponent(taskId)}`;
  const [taskRes, msgRes, inputRes, outputRes, artifactRes] = await Promise.all([
    requestV2(base),
    requestV2(messagesPath(taskId, 0)),
    requestV2(`${base}/files?direction=input`),
    requestV2(`${base}/files?direction=output`),
    requestV2(`${base}/artifacts`),
  ]);
  const task = taskRes.data?.task ?? null;
  if (!task) {
    throw new V2ApiError("INVALID_RESPONSE", "任务快照缺少 task 字段", taskRes.status);
  }
  return {
    task,
    messages: Array.isArray(msgRes.data) ? msgRes.data : [],
    inputFiles: Array.isArray(inputRes.data) ? inputRes.data : [],
    artifacts: Array.isArray(artifactRes.data) ? artifactRes.data : [],
  };
}

function describeSendError(cause) {
  if (cause instanceof V2ApiError && cause.code === "TASK_ROUND_BUSY") {
    const wait =
      Number.isFinite(cause.retryAfter) && cause.retryAfter > 0 ? `（约 ${cause.retryAfter} 秒后可重试）` : "";
    return `当前轮次尚未结束，请等待本轮回复完成后再发送${wait}`;
  }
  return cause?.message ?? "发送失败，请稍后重试";
}

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
