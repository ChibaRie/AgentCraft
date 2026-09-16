/**
 * V2 任务事件 SSE 客户端（D10，Phase 8 T8；契约 = Sup §1.2/§1.3 + Phase 8 计划 Task 8）。
 *
 * 冻结签名（工程审查 I2）：
 * - createTaskStream({taskId, after, onFrame, onEvents, onError}) -> {close()}
 *     onFrame({id, event, data})  逐帧回调；持久帧 id=event sequence，瞬态帧 id=null
 *     onEvents(events, snapshot)  重连对账补拉回调（快照 watermark 大于本地时）
 *     onError(err)                信封/网络错误（统一 V2ApiError）
 *     close()                     清理全部定时器与 AbortController（幂等）
 * - fetchEvents(taskId, after) -> {events, snapshot}（T9 消费的事实补拉面）
 *
 * 语义钉（与简报逐条对应）：
 * - fetch 流（非 EventSource）+ credentials same-origin（cookie 会话，无 Authorization）；
 * - 持久帧 `id:` 行推进水位；瞬态帧（无 id 行）不动水位；非正整数/非法 id 行整体
 *   忽略（安全审查 I-6.1：防 NaN 水位污染后 ?after=NaN 400 死循环烧穿 60/h 限流）；
 * - 心跳注释行 `: ping` 跳过；data 非 JSON → onError 不崩溃、该帧 id 不推进水位；
 * - 解析 buffer 设上限（V1 骨架 sse.js 无界累积的反面）：超限丢弃至下一帧边界；
 * - 断流 → 指数退避重连（1/2/4/8/…/30s 封顶 + 加性抖动只加不减）；429 读
 *   Retry-After 作退避下限（缺失/非法默认 ≥60s——安全 Minor 4）；
 * - 重连先 fetchEvents(taskId, lastSeq) 对账（快照 watermark 大于本地 lastSeq 则经
 *   onEvents 补拉缺帧并推进水位）再续流；
 * - 401 SESSION_EXPIRED 沿既有全局事件（D10）；其余 4xx（403/404 等非瞬时失败）
 *   不重连（避免烧穿 sse_connect 60/h 限流），5xx/网络错误/429 继续退避重连；
 * - 看门狗：流打开后 45s（3×15s 心跳周期）无任何字节 → 视为半开连接，中止重连。
 */

import {
  V2ApiError,
  V2_SESSION_EXPIRED_EVENT,
  requestV2,
  setCsrfToken,
} from "./client.js";
import { V2_TASKS } from "./routes.js";

// 与 client.js 同源语义（BASE_URL 未导出，本地同式重算——VITE_API_BASE_URL 部署覆盖）
const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

/** 单帧解析缓冲上限（字节）：超限丢弃至下一帧边界并报 SSE_FRAME_TOO_LARGE */
export const SSE_MAX_BUFFER_BYTES = 1024 * 1024;
export const SSE_MAX_BACKOFF_SECONDS = 30;
/** 429 Retry-After 缺失/非法时的退避下限（秒）——安全 Minor 4 */
export const SSE_DEFAULT_RETRY_AFTER_SECONDS = 60;
/** 看门狗超时（秒）：3 × 服务端 15s 心跳周期无字节即判半开连接 */
export const SSE_WATCHDOG_SECONDS = 45;
/** 加性抖动比例（只加不减，random=0 时退避序列恰为 1/2/4/8…） */
export const SSE_JITTER_FRACTION = 0.3;

/** /events 补拉页大小（后端上限 1000） */
const EVENTS_PAGE_LIMIT = 1000;
/** 合法 id 行 = 十进制正整数（0、负数、非数字、超安全整数一律忽略） */
const POSITIVE_SEQ_RE = /^\d+$/;

/**
 * 指数退避（秒序列 1/2/4/8/…/30 封顶）与 429 下限取大，再施加加性抖动。
 * failureIndex 从 0 起（首次失败 1s）；floorSeconds>0 时目标值取 max(base, floor)。
 */
function nextBackoffMs(failureIndex, floorSeconds = 0) {
  const baseSeconds = Math.min(
    SSE_MAX_BACKOFF_SECONDS,
    2 ** failureIndex
  );
  const floorMs = floorSeconds > 0 ? floorSeconds * 1000 : 0;
  const target = Math.max(baseSeconds * 1000, floorMs);
  return target + Math.random() * target * SSE_JITTER_FRACTION;
}

/**
 * 解析单个 SSE 块（空行分隔）：`id:`/`event:`/`data:` 行 + 注释行。
 * 返回 {id, event, data}；纯注释/空块返回 null；data 非 JSON 返回 {error}。
 * id 行非正整数 → 视同瞬态（id=null），水位不动（I-6.1）。
 */
function parseFrame(block) {
  let id = null;
  let event = "message";
  const dataLines = [];
  let sawData = false;
  for (const rawLine of block.split("\n")) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (line === "" || line.startsWith(":")) {
      continue; // 空行残片 / 注释行（`: ping` 心跳走此分支）
    }
    if (line.startsWith("id:")) {
      const raw = line.slice(3).trim();
      if (POSITIVE_SEQ_RE.test(raw)) {
        const parsed = Number(raw);
        if (Number.isSafeInteger(parsed) && parsed > 0) {
          id = parsed;
        }
      }
      continue; // 非法 id 行整体忽略（不污染水位）
    }
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
      continue;
    }
    if (line.startsWith("data:")) {
      sawData = true;
      dataLines.push(line.slice(5).replace(/^ /, "")); // SSE 规范：剥单个前导空格
      continue;
    }
    // 未知字段行：SSE 规范要求忽略
  }
  if (!sawData) {
    return null;
  }
  try {
    return { id, event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return {
      error: new V2ApiError("SSE_PARSE_ERROR", "事件帧数据不是有效 JSON", 0),
    };
  }
}

/** 流建立失败的信封解析：统一 V2ApiError（code/message/status；429 附 retryAfter） */
async function parseEnvelopeError(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    // 非 JSON 响应体（如代理错误页）——按状态码兜底
  }
  const code = payload?.error?.code ?? `HTTP_${response.status}`;
  const message = payload?.error?.message ?? `请求失败（HTTP ${response.status}）`;
  let retryAfter;
  if (response.status === 429) {
    const header = response.headers.get("Retry-After");
    const seconds = header === null ? Number.NaN : Number(header);
    if (!Number.isNaN(seconds)) {
      retryAfter = seconds;
    }
  }
  return new V2ApiError(code, message, response.status, retryAfter);
}

function toNetworkError() {
  return new V2ApiError("NETWORK_ERROR", "事件流连接中断", 0);
}

/**
 * 事实补拉（Sup §1.2:27）：GET /events?after=&limit=1000，升序事件帧 + 当前快照。
 * 走 requestV2 复用信封/会话过期语义；帧形状与 SSE 重放一致（含 sequence 传输键）。
 */
export async function fetchEvents(taskId, after = 0) {
  if (!taskId) {
    throw new TypeError("fetchEvents：taskId 必填");
  }
  const seq = Number.isInteger(after) && after > 0 ? after : 0;
  const result = await requestV2(
    `${V2_TASKS}/${encodeURIComponent(String(taskId))}/events?after=${seq}&limit=${EVENTS_PAGE_LIMIT}`
  );
  const data = result.data ?? {};
  return {
    events: Array.isArray(data.events) ? data.events : [],
    snapshot: data.snapshot ?? null,
  };
}

/**
 * 实时事件流（D10）。连接建立成功即重置退避计数；断流/失败进入退避重连循环，
 * 重连时先 /events 对账再续流。close() 后一切归零（不再发请求、无残余定时器）。
 */
export function createTaskStream({ taskId, after = 0, onFrame, onEvents, onError }) {
  const taskIdText = String(taskId ?? "").trim();
  if (!taskIdText) {
    throw new TypeError("createTaskStream：taskId 必填");
  }

  let lastSeq = Number.isInteger(after) && after > 0 ? after : 0;
  let attempt = 0; // 连续失败计数（连接成功归零）；退避取 2^attempt
  let closed = false;
  let controller = null; // 当前连接的 AbortController
  let reconnectTimer = null;
  let watchdogTimer = null;

  function safeInvoke(callback, ...args) {
    if (typeof callback !== "function") {
      return;
    }
    try {
      callback(...args);
    } catch (cause) {
      // 消费方回调缺陷不得杀死读取循环——转投 onError；onError 自身抛错到此为止
      if (callback !== onError) {
        try {
          if (typeof onError === "function") {
            onError(cause);
          }
        } catch {
          // 无下游可报：保持流存活（静默是唯一不至于断流的选择）
        }
      }
    }
  }

  function clearTimers() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (watchdogTimer !== null) {
      clearTimeout(watchdogTimer);
      watchdogTimer = null;
    }
  }

  function close() {
    if (closed) {
      return;
    }
    closed = true;
    clearTimers();
    if (controller !== null) {
      controller.abort(); // 挂起的 fetch/reader 拒绝 → 各 await 点按 closed 静默
    }
  }

  function scheduleReconnect(delayMs) {
    if (closed) {
      return;
    }
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (!closed) {
        void reconcileAndReconnect();
      }
    }, delayMs);
  }

  /** 失败登记：按当前失败序计算退避（1/2/4/8…）后计数前移，再排定重连 */
  function registerFailure({ floorSeconds = 0 } = {}) {
    scheduleReconnect(nextBackoffMs(attempt, floorSeconds));
    attempt += 1;
  }

  /** 看门狗：流打开期间每收到一块数据续期；超时判半开连接 → 中止走重连 */
  function armWatchdog() {
    if (watchdogTimer !== null) {
      clearTimeout(watchdogTimer);
    }
    watchdogTimer = setTimeout(() => {
      watchdogTimer = null;
      if (!closed && controller !== null) {
        controller.abort();
      }
    }, SSE_WATCHDOG_SECONDS * 1000);
  }

  function dispatchBlock(block) {
    const parsed = parseFrame(block);
    if (parsed === null) {
      return; // 纯注释/空块（心跳）
    }
    if (parsed.error) {
      safeInvoke(onError, parsed.error); // data 非 JSON：不崩溃、水位不动
      return;
    }
    if (parsed.id !== null) {
      lastSeq = Math.max(lastSeq, parsed.id); // 只有持久帧推进水位
    }
    safeInvoke(onFrame, parsed);
  }

  async function consume(body) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let discarding = false; // 超限帧丢弃模式：只找边界，不进解析
    armWatchdog();
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      armWatchdog(); // 心跳/帧均续命
      buffer += decoder.decode(value, { stream: true });
      for (;;) {
        const boundary = buffer.indexOf("\n\n");
        if (discarding) {
          if (boundary === -1) {
            buffer = ""; // 丢弃模式无界内存的止位
            break;
          }
          buffer = buffer.slice(boundary + 2);
          discarding = false;
          continue;
        }
        if (boundary === -1) {
          if (buffer.length > SSE_MAX_BUFFER_BYTES) {
            buffer = "";
            discarding = true;
            safeInvoke(
              onError,
              new V2ApiError("SSE_FRAME_TOO_LARGE", "事件帧超限，已丢弃超大帧", 0)
            );
          }
          break;
        }
        dispatchBlock(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
      }
    }
  }

  async function connect() {
    if (closed) {
      return;
    }
    controller = new AbortController();
    let response;
    try {
      response = await fetch(
        `${BASE_URL}${V2_TASKS}/${encodeURIComponent(taskIdText)}/events/stream?after=${lastSeq}`,
        {
          method: "GET",
          headers: { Accept: "text/event-stream" },
          credentials: "same-origin",
          cache: "no-store",
          signal: controller.signal,
        }
      );
    } catch {
      disarmWatchdog();
      if (closed) {
        return; // close() 触发的 abort：静默
      }
      safeInvoke(onError, toNetworkError());
      registerFailure();
      return;
    }

    if (!response.ok || !response.body) {
      const error = await parseEnvelopeError(response);
      if (response.status === 401 && error.code === "SESSION_EXPIRED") {
        // D10：会话过期沿既有全局事件（与 requestV2 finalize 同语义）
        setCsrfToken(null);
        window.dispatchEvent(
          new CustomEvent(V2_SESSION_EXPIRED_EVENT, {
            detail: { path: `${V2_TASKS}/${taskIdText}/events/stream` },
          })
        );
      }
      safeInvoke(onError, error);
      if (response.status === 429) {
        const floor =
          Number.isFinite(error.retryAfter) && error.retryAfter > 0
            ? error.retryAfter
            : SSE_DEFAULT_RETRY_AFTER_SECONDS;
        registerFailure({ floorSeconds: floor });
      } else if (response.status >= 500) {
        registerFailure();
      }
      // 其余 4xx（401/403/404 等非瞬时失败）：不重连，停止烧限流，由消费方处置
      return;
    }

    // 连接成功：退避计数归零，进入读取循环
    attempt = 0;
    try {
      await consume(response.body);
    } catch {
      if (closed) {
        return; // close()/看门狗以外的 abort 亦按关闭静默
      }
      safeInvoke(onError, toNetworkError());
    } finally {
      disarmWatchdog();
    }
    if (closed) {
      return;
    }
    registerFailure(); // 流正常结束（done）也属断流 → 退避重连
  }

  function disarmWatchdog() {
    if (watchdogTimer !== null) {
      clearTimeout(watchdogTimer);
      watchdogTimer = null;
    }
  }

  /** 重连周期：先 /events 对账（补拉缺帧并推进水位）再续流（D10 调用序钉） */
  async function reconcileAndReconnect() {
    try {
      const { events, snapshot } = await fetchEvents(taskIdText, lastSeq);
      if (closed) {
        return;
      }
      const watermark = Number(snapshot?.event_sequence);
      if (Number.isFinite(watermark) && watermark > lastSeq) {
        safeInvoke(onEvents, events, snapshot); // 快照 watermark 大于本地 → 补拉缺帧
        lastSeq = Math.max(lastSeq, watermark); // 对账后水位对齐权威快照
      }
    } catch (cause) {
      if (closed) {
        return;
      }
      safeInvoke(onError, cause instanceof V2ApiError ? cause : toNetworkError());
      registerFailure();
      return;
    }
    if (closed) {
      return;
    }
    await connect();
  }

  void connect();

  return { close };
}
