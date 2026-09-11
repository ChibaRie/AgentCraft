/**
 * V2 API 客户端层（FE-T1）：cookie 会话 + CSRF 双提交 + 幂等键 + 统一信封。
 *
 * 后端契约（只读参考）：
 * - backend/v2/session_service.py：ac_session（HttpOnly）/ac_csrf（非 HttpOnly，
 *   SPA 读取后回填 X-CSRF-Token）；写方法 POST/PUT/DELETE/PATCH 须带 CSRF 头；
 *   失效统一 401 SESSION_EXPIRED，CSRF 失败 403 CSRF_INVALID，状态门 403
 *   ACCOUNT_SUSPENDED / ACCOUNT_DELETING。
 * - backend/v2/idempotency.py：义务端点须带 Idempotency-Key（≤100 字符），
 *   缺失 → 400 VALIDATION_ERROR。
 * - backend/main.py：成功信封 {data: ...}，失败信封 {error: {code, message}}，
 *   错误 headers 透传（429 → Retry-After）。
 *
 * 与 V1 client.js 的关系：独立并存，V2 面（/api/v2/**）一律走本模块；
 * V1 面继续走 request()（Bearer token 语义），互不污染。
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const CSRF_COOKIE_NAME = "ac_csrf";

// 与后端 _CSRF_METHODS（session_service.py）对齐：写方法过 CSRF 门，只读方法免检
const CSRF_METHODS = new Set(["POST", "PUT", "DELETE", "PATCH"]);

/** 401 SESSION_EXPIRED 时（非 silent）派发的事件名；handler 在 FE-T2 实现 */
export const V2_SESSION_EXPIRED_EVENT = "v2:session-expired";

/**
 * 幂等义务端点路径模式表（漏接 warn；与 backend/api/v2 各路由的
 * Depends(require_key_header) 一一对应）。命中且未显式传 idempotencyKey 时
 * console.warn——服务端将以 400 拒绝，属接线遗漏而非运行时分支。
 */
export const V2_IDEMPOTENCY_REQUIRED = [
  /^\/api\/v2\/providers$/,
  /^\/api\/v2\/providers\/[^/]+$/,
  /^\/api\/v2\/auth\/sessions\/[^/]+$/,
  /^\/api\/v2\/auth\/invitations\/accept$/,
  /^\/api\/v2\/auth\/email-verification\/confirm$/,
  /^\/api\/v2\/auth\/password-reset\/confirm$/,
  /^\/api\/v2\/account\/deletion\/request$/,
  /^\/api\/v2\/account\/deletion\/cancel$/,
];

let memoryCsrfToken = null;

/** 设置内存态 CSRF 令牌；null/空 = 清除（登录后种入、401 后清除） */
export function setCsrfToken(token) {
  memoryCsrfToken = token || null;
}

function readCsrfCookie() {
  const entry = document.cookie
    .split("; ")
    .find((chunk) => chunk.startsWith(`${CSRF_COOKIE_NAME}=`));
  if (!entry) {
    return null;
  }
  return decodeURIComponent(entry.slice(CSRF_COOKIE_NAME.length + 1)) || null;
}

/** 内存优先，回退读 ac_csrf cookie（非 HttpOnly 交付信道，A14） */
export function getCsrfToken() {
  return memoryCsrfToken ?? readCsrfCookie();
}

/** V2 统一错误：{ code, message, status, retryAfter? }（retryAfter 仅 429 存在） */
export class V2ApiError extends Error {
  constructor(code, message, status, retryAfter) {
    super(message);
    this.name = "V2ApiError";
    this.code = code;
    this.status = status;
    if (retryAfter !== undefined) {
      this.retryAfter = retryAfter;
    }
  }
}

function isIdempotencyRequiredPath(path) {
  return V2_IDEMPOTENCY_REQUIRED.some((pattern) => pattern.test(path));
}

function buildRequestInit(path, options, { forceCookieCsrf = false } = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {};

  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  if (CSRF_METHODS.has(method)) {
    // forceCookieCsrf：CSRF_INVALID 重试时内存态必然过期，必须直读 cookie 新令牌
    const csrf = forceCookieCsrf ? readCsrfCookie() : getCsrfToken();
    if (csrf) {
      headers["X-CSRF-Token"] = csrf;
    }
    if (options.idempotencyKey) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    } else if (isIdempotencyRequiredPath(path)) {
      console.warn(
        `[v2-client] ${method} ${path} 为幂等义务端点但未提供 idempotencyKey` +
          "（Idempotency-Key 头），服务端将以 400 VALIDATION_ERROR 拒绝（接线遗漏）"
      );
    }
  }

  return {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
    credentials: "same-origin",
  };
}

async function parseJson(response) {
  return response.json().catch(() => null);
}

function finalize(response, payload, { path, silent }) {
  if (!response.ok) {
    const code = payload?.error?.code ?? `HTTP_${response.status}`;
    const message = payload?.error?.message ?? `请求失败（HTTP ${response.status}）`;

    // 仅 code === "SESSION_EXPIRED" 触发（V1 的 401 UNAUTHORIZED 不派发事件）；
    // silent:true 为探测/会话判定类调用——仍清 csrf 并抛错，但不派发（FE-T2 接 handler）
    if (response.status === 401 && code === "SESSION_EXPIRED") {
      setCsrfToken(null);
      if (!silent) {
        window.dispatchEvent(
          new CustomEvent(V2_SESSION_EXPIRED_EVENT, { detail: { path } })
        );
      }
    }

    let retryAfter;
    if (response.status === 429) {
      const header = response.headers.get("Retry-After");
      const seconds = header === null ? Number.NaN : Number(header);
      if (!Number.isNaN(seconds)) {
        retryAfter = seconds;
      }
    }
    throw new V2ApiError(code, message, response.status, retryAfter);
  }

  if (payload === null || typeof payload !== "object") {
    // 2xx 但响应体不是 JSON（如静态托管回退页）
    throw new V2ApiError("INVALID_RESPONSE", "响应格式错误，请稍后重试", response.status);
  }

  // 成功信封解包 data；信封顶层 total/page/size（GET /auth/sessions）丢弃——
  // T7 只渲染列表，后续任务不得假设可取 total
  return { status: response.status, data: payload.data ?? null, headers: response.headers };
}

/**
 * V2 统一请求。
 * @param {string} path 完整 V2 路径（如 `${V2_PROVIDERS}`，含 /api/v2 前缀）
 * @param {object} [options]
 * @param {string} [options.method="GET"]
 * @param {object} [options.body] JSON 序列化后发送
 * @param {string} [options.idempotencyKey] 义务端点必传（漏传有 warn）
 * @param {AbortSignal} [options.signal]
 * @param {boolean} [options.silent] true 时 401 SESSION_EXPIRED 不派发事件（探测模式）
 * @returns {Promise<{status: number, data: object|null, headers: Headers}>}
 * @throws {V2ApiError}
 */
export async function requestV2(path, options = {}) {
  const url = `${BASE_URL}${path}`;
  let response = await fetch(url, buildRequestInit(path, options));
  let payload = await parseJson(response);

  // CSRF 门重试：判据是响应体 code === "CSRF_INVALID"（不按 status 403 判断，
  // 其它 403 如 ACCOUNT_SUSPENDED 不重试直接抛）。最多 1 次，复用原 idempotencyKey；
  // 重试直读 cookie 新令牌，成功后回写内存——防内存/cookie 脱钩后每个写请求永久双往返。
  if (payload?.error?.code === "CSRF_INVALID") {
    const freshToken = readCsrfCookie();
    response = await fetch(url, buildRequestInit(path, options, { forceCookieCsrf: true }));
    payload = await parseJson(response);
    if (response.ok && freshToken) {
      setCsrfToken(freshToken);
    }
  }

  return finalize(response, payload, { path, silent: options.silent === true });
}
