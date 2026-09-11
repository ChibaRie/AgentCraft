import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  V2ApiError,
  V2_IDEMPOTENCY_REQUIRED,
  V2_SESSION_EXPIRED_EVENT,
  getCsrfToken,
  requestV2,
  setCsrfToken,
} from "./client.js";
import { V2_ACCOUNT, V2_AUTH, V2_PROVIDERS, V2_USERS } from "./routes.js";

const CSRF_COOKIE = "ac_csrf";

function setCsrfCookie(value) {
  document.cookie = `${CSRF_COOKIE}=${value}; path=/`;
}

function clearCsrfCookie() {
  document.cookie = `${CSRF_COOKIE}=; Max-Age=0; path=/`;
}

/** 可编程响应：默认 JSON 信封；headers 允许传入 Headers 实例（429 Retry-After 用例） */
function jsonResponse(body, { status = 200, headers } = {}) {
  const init = { status };
  if (headers !== undefined) {
    init.headers = headers;
  } else {
    init.headers = { "Content-Type": "application/json" };
  }
  return new Response(JSON.stringify(body), init);
}

/** vi.stubGlobal + mockImplementationOnce 队列 = 可编程顺序响应 */
function stubFetch(...responses) {
  const fetchMock = vi.fn();
  for (const response of responses) {
    fetchMock.mockImplementationOnce(() => Promise.resolve(response));
  }
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** 读取某次 fetch 调用的请求头（兼容普通对象与 Headers 实例两种 init 形态） */
function requestHeaders(call) {
  const headers = call[1].headers;
  if (headers instanceof Headers) {
    return Object.fromEntries(headers.entries());
  }
  return { ...headers };
}

function trackSessionExpired() {
  const handler = vi.fn();
  window.addEventListener(V2_SESSION_EXPIRED_EVENT, handler);
  return function stop() {
    window.removeEventListener(V2_SESSION_EXPIRED_EVENT, handler);
  };
}

beforeEach(() => {
  setCsrfToken(null);
  clearCsrfCookie();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("routes", () => {
  it("导出四组 V2 路由前缀常量（后续任务消费的精确值）", () => {
    expect(V2_AUTH).toBe("/api/v2/auth");
    expect(V2_ACCOUNT).toBe("/api/v2/account");
    expect(V2_USERS).toBe("/api/v2/users");
    expect(V2_PROVIDERS).toBe("/api/v2/providers");
  });
});

describe("requestV2 · CSRF 注入", () => {
  // 用例 1
  it("写方法注入 CSRF：内存态优先于 cookie", async () => {
    setCsrfToken("memory-token");
    setCsrfCookie("cookie-token");
    const fetchMock = stubFetch(jsonResponse({ data: { ok: true } }), jsonResponse({ data: {} }));

    await requestV2(V2_PROVIDERS, { method: "POST", body: {} });
    await requestV2(V2_PROVIDERS);

    const postHeaders = requestHeaders(fetchMock.mock.calls[0]);
    expect(postHeaders["X-CSRF-Token"]).toBe("memory-token");

    // GET/HEAD/OPTIONS 免 CSRF 门（与后端 _CSRF_METHODS 对齐）
    const getHeaders = requestHeaders(fetchMock.mock.calls[1]);
    expect(getHeaders).not.toHaveProperty("X-CSRF-Token");
  });

  // 用例 2
  it("内存态缺失时回退读 document.cookie 的 ac_csrf", async () => {
    setCsrfCookie("cookie-fallback");
    const fetchMock = stubFetch(jsonResponse({ data: { ok: true } }));

    await requestV2(V2_PROVIDERS, { method: "POST", body: {} });

    expect(requestHeaders(fetchMock.mock.calls[0])["X-CSRF-Token"]).toBe("cookie-fallback");
  });

  it("getCsrfToken 同样内存优先、回退 cookie，且无值时返回 null", () => {
    expect(getCsrfToken()).toBeNull();
    setCsrfCookie("from-cookie");
    expect(getCsrfToken()).toBe("from-cookie");
    setCsrfToken("from-memory");
    expect(getCsrfToken()).toBe("from-memory");
  });
});

describe("requestV2 · 幂等键", () => {
  // 用例 3
  it("显式 idempotencyKey 注入 Idempotency-Key 头；义务端点漏 key → console.warn", async () => {
    const fetchMock = stubFetch(jsonResponse({ data: { ok: true } }), jsonResponse({ data: {} }));

    await requestV2(V2_PROVIDERS, { method: "POST", body: {}, idempotencyKey: "key-abc" });
    expect(requestHeaders(fetchMock.mock.calls[0])["Idempotency-Key"]).toBe("key-abc");

    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    await requestV2(V2_PROVIDERS, { method: "POST", body: {} });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(String(warnSpy.mock.calls[0][0])).toContain("Idempotency-Key");
  });

  it("V2_IDEMPOTENCY_REQUIRED 覆盖后端 9 个义务端点且不误报非义务路径", () => {
    // 与 backend/api/v2/{auth,account,providers}.py 的 Depends(require_key_header) 端点一一对应
    const mandatoryPaths = [
      "/api/v2/providers",
      "/api/v2/providers/p1",
      "/api/v2/auth/sessions/s1",
      "/api/v2/auth/invitations/accept",
      "/api/v2/auth/email-verification/confirm",
      "/api/v2/auth/password-reset/confirm",
      "/api/v2/account/deletion/request",
      "/api/v2/account/deletion/cancel",
    ];
    for (const path of mandatoryPaths) {
      expect(V2_IDEMPOTENCY_REQUIRED.some((re) => re.test(path))).toBe(true);
    }

    const nonMandatoryPaths = [
      "/api/v2/auth/login",
      "/api/v2/auth/login/mfa",
      "/api/v2/auth/logout",
      "/api/v2/auth/email-verification/resend",
      "/api/v2/auth/password-reset/request",
      "/api/v2/auth/password-change",
      "/api/v2/auth/mfa/setup",
      "/api/v2/auth/mfa/activate",
      "/api/v2/auth/sessions",
      "/api/v2/providers/p1/test",
    ];
    for (const path of nonMandatoryPaths) {
      expect(V2_IDEMPOTENCY_REQUIRED.some((re) => re.test(path))).toBe(false);
    }
  });
});

describe("requestV2 · 401 会话失效", () => {
  // 用例 4
  it("401 SESSION_EXPIRED → 派发 v2:session-expired + 清 csrf + 抛 V2ApiError", async () => {
    setCsrfToken("soon-invalid");
    stubFetch(
      jsonResponse(
        { error: { code: "SESSION_EXPIRED", message: "登录状态已失效，请重新登录" } },
        { status: 401 }
      )
    );
    const stop = trackSessionExpired();

    let error;
    try {
      error = await requestV2(`${V2_AUTH}/me`).catch((e) => e);
      expect(getCsrfToken()).toBeNull();
    } finally {
      stop();
    }

    expect(error).toBeInstanceOf(V2ApiError);
    expect(error.code).toBe("SESSION_EXPIRED");
    expect(error.status).toBe(401);
    expect(error.message).toBe("登录状态已失效，请重新登录");
  });

  // 用例 4a
  it("silent:true 的 401 SESSION_EXPIRED → 不派发事件，仍清 csrf 并抛 V2ApiError", async () => {
    setCsrfToken("probe-token");
    stubFetch(
      jsonResponse(
        { error: { code: "SESSION_EXPIRED", message: "登录状态已失效，请重新登录" } },
        { status: 401 }
      )
    );
    const stop = trackSessionExpired();

    let error;
    try {
      error = await requestV2(`${V2_AUTH}/me`, { silent: true }).catch((e) => e);
      expect(getCsrfToken()).toBeNull();
    } finally {
      stop();
    }

    expect(error).toBeInstanceOf(V2ApiError);
    expect(error.code).toBe("SESSION_EXPIRED");
  });

  // 用例 5
  it("401 但 code≠SESSION_EXPIRED（V1 语义 UNAUTHORIZED）不派发事件", async () => {
    stubFetch(jsonResponse({ error: { code: "UNAUTHORIZED", message: "未登录" } }, { status: 401 }));
    const stop = trackSessionExpired();

    let error;
    try {
      error = await requestV2(`${V2_AUTH}/me`).catch((e) => e);
    } finally {
      stop();
    }

    expect(error).toBeInstanceOf(V2ApiError);
    expect(error.code).toBe("UNAUTHORIZED");
  });
});

describe("requestV2 · 403 CSRF 重试", () => {
  // 用例 6
  it("403 CSRF_INVALID → 重读 cookie 重试一次（同幂等键），二次仍 403 → 抛错", async () => {
    setCsrfToken("stale-memory-token");
    setCsrfCookie("fresh-cookie-token");
    const csrfError = { error: { code: "CSRF_INVALID", message: "CSRF 校验失败" } };
    const fetchMock = stubFetch(
      jsonResponse(csrfError, { status: 403 }),
      jsonResponse(csrfError, { status: 403 })
    );

    await expect(
      requestV2(V2_PROVIDERS, { method: "POST", body: {}, idempotencyKey: "idem-1" })
    ).rejects.toMatchObject({ code: "CSRF_INVALID", status: 403 });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    // 首次：内存态 token
    expect(requestHeaders(fetchMock.mock.calls[0])["X-CSRF-Token"]).toBe("stale-memory-token");
    // 重试：重读 cookie 的新 token，且复用原幂等键
    expect(requestHeaders(fetchMock.mock.calls[1])["X-CSRF-Token"]).toBe("fresh-cookie-token");
    expect(requestHeaders(fetchMock.mock.calls[1])["Idempotency-Key"]).toBe("idem-1");
  });

  // 用例 6a：重试判据是响应体 code==="CSRF_INVALID"，而非 status===403
  it("403 但 code≠CSRF_INVALID（ACCOUNT_SUSPENDED）→ 不重试直接抛", async () => {
    setCsrfToken("mem-token");
    setCsrfCookie("would-be-reread");
    const fetchMock = stubFetch(
      jsonResponse({ error: { code: "ACCOUNT_SUSPENDED", message: "账户已被停用" } }, { status: 403 })
    );
    const stop = trackSessionExpired();

    let error;
    try {
      error = await requestV2(V2_PROVIDERS, { method: "POST", body: {} }).catch((e) => e);
    } finally {
      stop();
    }

    expect(error).toBeInstanceOf(V2ApiError);
    expect(error.code).toBe("ACCOUNT_SUSPENDED");
    expect(error.status).toBe(403);
    // 恰好 1 次 fetch：无重试（自然也无 cookie 重读、无事件派发）
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(requestHeaders(fetchMock.mock.calls[0])["X-CSRF-Token"]).toBe("mem-token");
  });

  // 用例 6b（Step 4 行为钉死）：重试成功后 setCsrfToken 回写内存，防永久双往返
  it("CSRF_INVALID 重试成功 → 新 token 回写内存", async () => {
    setCsrfToken("stale-memory-token");
    setCsrfCookie("fresh-cookie-token");
    const fetchMock = stubFetch(
      jsonResponse(
        { error: { code: "CSRF_INVALID", message: "CSRF 校验失败" } },
        { status: 403 }
      ),
      jsonResponse({ data: { ok: true } })
    );

    const result = await requestV2(V2_PROVIDERS, { method: "POST", body: {} });

    expect(result.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // 清掉 cookie 后仍能取到 → 新 token 已写入内存态（而非仅 cookie 回退）
    clearCsrfCookie();
    expect(getCsrfToken()).toBe("fresh-cookie-token");
  });
});

describe("requestV2 · 429 限流", () => {
  // 用例 7：mock 必须用 new Headers({...})（真实 fetch 的 headers 是 Headers 实例）
  it("429 → V2ApiError.retryAfter = Number(headers.get('Retry-After'))", async () => {
    stubFetch(
      jsonResponse(
        { error: { code: "TOO_MANY_REQUESTS", message: "请求过于频繁，请稍后重试" } },
        { status: 429, headers: new Headers({ "Retry-After": "30" }) }
      )
    );

    await expect(requestV2(`${V2_AUTH}/me`)).rejects.toMatchObject({
      code: "TOO_MANY_REQUESTS",
      status: 429,
      retryAfter: 30,
    });
  });
});

describe("requestV2 · 信封", () => {
  // 用例 8
  it("成功解包 {data:...}（信封顶层 total/page/size 丢弃）；失败抛 {code,message}", async () => {
    stubFetch(jsonResponse({ data: { id: "u1", email: "a@b.c" } }));
    const ok = await requestV2(`${V2_AUTH}/me`);
    expect(ok.status).toBe(200);
    expect(ok.data).toEqual({ id: "u1", email: "a@b.c" });

    // GET /auth/sessions 形态：total/page/size 在信封顶层，客户端丢弃——T7 不得假设可取
    stubFetch(jsonResponse({ data: [{ id: "s1" }], total: 1, page: 1, size: 20 }));
    const sessions = await requestV2(`${V2_AUTH}/sessions`);
    expect(sessions.data).toEqual([{ id: "s1" }]);
    expect(sessions).not.toHaveProperty("total");

    stubFetch(jsonResponse({ error: { code: "NOT_FOUND", message: "资源不存在" } }, { status: 404 }));
    await expect(requestV2(`${V2_AUTH}/me`)).rejects.toMatchObject({
      code: "NOT_FOUND",
      message: "资源不存在",
      status: 404,
    });
  });
});

describe("requestV2 · 请求形态", () => {
  // 用例 9
  it("credentials:'same-origin' 存在于每个请求 init", async () => {
    stubFetch(jsonResponse({ data: {} }), jsonResponse({ data: {} }));

    await requestV2(`${V2_AUTH}/me`);
    await requestV2(V2_PROVIDERS, { method: "POST", body: {} });

    expect(fetch).toHaveBeenCalledTimes(2);
    for (const call of fetch.mock.calls) {
      expect(call[1].credentials).toBe("same-origin");
    }
  });

  it("body 对象序列化为 JSON 字符串并带 Content-Type", async () => {
    const fetchMock = stubFetch(jsonResponse({ data: { ok: true } }));

    await requestV2(`${V2_AUTH}/login`, { method: "POST", body: { email: "a@b.c" } });

    const init = fetchMock.mock.calls[0][1];
    expect(init.body).toBe(JSON.stringify({ email: "a@b.c" }));
    expect(init.headers["Content-Type"]).toBe("application/json");
  });
});
