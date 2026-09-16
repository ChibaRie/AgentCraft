import { V2ApiError } from "../api/v2/client.js";
import { V2_TASKS } from "../api/v2/routes.js";

/**
 * P09 任务对话页状态模型（Phase 9 T7 自 TaskChatPage.jsx 外移）。
 *
 * 纯函数区：常量词表 + 水位单调 reducer + 展示用错误文案。无 I/O、无 React
 * 依赖——可独立直测（TaskChatPage.test.jsx 的 reducer 单调约束用例）。
 *
 * 数据编排语义见 TaskChatPage.jsx 页头 docstring（装配层在 taskChatBundle.js）。
 */

export const TERMINAL_STATUSES = new Set(["completed", "failed", "aborted", "deleted"]);
// abort/complete 的受理状态集（Sup §1.2:29/30——uploading 用 DELETE 终态化，终态 409）
export const ACTIONABLE_STATUSES = ["queued", "running", "ready"];
export const MESSAGE_MAX_CHARS = 65536;
const MESSAGES_PAGE_LIMIT = 200;
export const SIDEBAR_PAGE_SIZE = 50;
// 用户上翻回看时停止自动滚底的容差（像素）
export const AUTOSCROLL_THRESHOLD_PX = 120;

export const ABORT_REASON_LABELS = {
  user_cancel: "用户取消",
  round_failed: "执行失败",
  upload_expired: "上传超时",
  provider_key_revoked: "Provider 凭据已撤销",
  tool_revoked: "工具已停用",
  admin_suspended: "账号被停用",
};

const EMPTY_STREAMING = Object.freeze({ active: false, text: "", thinking: "", toolCalls: [] });

export function messagesPath(taskId, after) {
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

export function describeSendError(cause) {
  if (cause instanceof V2ApiError && cause.code === "TASK_ROUND_BUSY") {
    const wait =
      Number.isFinite(cause.retryAfter) && cause.retryAfter > 0 ? `（约 ${cause.retryAfter} 秒后可重试）` : "";
    return `当前轮次尚未结束，请等待本轮回复完成后再发送${wait}`;
  }
  return cause?.message ?? "发送失败，请稍后重试";
}
