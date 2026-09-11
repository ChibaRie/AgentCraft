import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getToken, request, setToken } from "../api/client.js";
import { V2_SESSION_EXPIRED_EVENT, V2ApiError, requestV2, setCsrfToken } from "../api/v2/client.js";
import { V2_AUTH, V2_USERS } from "../api/v2/routes.js";
import { AuthProvider, useAuth } from "./AuthContext.jsx";
import { displayName } from "./displayName.js";

vi.mock("../api/client.js", () => ({
  getToken: vi.fn(),
  setToken: vi.fn(),
  request: vi.fn(),
}));

// 只 mock 网络入口 requestV2 与 csrf 写口；V2ApiError / V2_SESSION_EXPIRED_EVENT 用真实导出
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn(), setCsrfToken: vi.fn() };
});

const V1_USER = {
  id: 1,
  username: "alice",
  email: "alice@example.com",
  role: "user",
  created_at: "2026-01-01T00:00:00Z",
};

// users/me / login 信封内的 V2 user 原始形状（snake_case；display_name 为映射观察点）
const V2_USER_RAW = {
  id: "u-2",
  email: "v2@example.com",
  role: "user",
  status: "active",
  display_name: "V2酱",
};

const V2_USER_MAPPED = {
  id: "u-2",
  email: "v2@example.com",
  role: "user",
  status: "active",
  displayName: "V2酱",
};

const SESSION_EXPIRED_401 = () =>
  new V2ApiError("SESSION_EXPIRED", "登录状态已失效，请重新登录", 401);

/** login / login/mfa 成功信封形态（backend/v2/login_service.py _login_body） */
function loginOk(data = { user: V2_USER_RAW, csrf_token: "csrf-tok" }) {
  return { status: 200, data, headers: new Headers() };
}

function renderUseAuth() {
  return renderHook(() => useAuth(), { wrapper: AuthProvider });
}

/**
 * jsdom 的 location 成员全是 unforgeable own 属性（non-writable + non-configurable，
 * spyOn/defineProperty/原型替换均实测不可行）；唯一替换口是 vitest 把 window 各键
 * 以可配置形式拷贝到 globalThis——stub 后 window.location 命中 stub 本体。
 */
function stubLocation(pathname = "/") {
  const locationStub = {
    href: `http://localhost:3000${pathname}`,
    pathname,
    assign: vi.fn(),
    replace: vi.fn(),
  };
  vi.stubGlobal("location", locationStub);
  return locationStub;
}

function fireSessionExpired() {
  act(() => {
    window.dispatchEvent(
      new CustomEvent(V2_SESSION_EXPIRED_EVENT, { detail: { path: `${V2_USERS}/me` } })
    );
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  getToken.mockReturnValue(null);
  request.mockRejectedValue(new Error("V1 request：本用例未显式编排"));
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
  stubLocation("/");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("启动探测双轨", () => {
  // 用例 ①
  it("V1 token 探测 200 + V2 探测 401 silent → v1User 在、v2User=null、双 ready、无跳转", async () => {
    const location = stubLocation("/");
    getToken.mockReturnValue("v1-token");
    request.mockResolvedValueOnce({ data: V1_USER });
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());

    const { result } = renderUseAuth();
    await waitFor(() => {
      expect(result.current.v1Ready).toBe(true);
      expect(result.current.v2Ready).toBe(true);
    });

    expect(result.current.user).toEqual(V1_USER);
    expect(result.current.v2User).toBeNull();
    expect(result.current.authReady).toBe(true);
    expect(requestV2).toHaveBeenCalledTimes(1);
    expect(requestV2).toHaveBeenCalledWith(`${V2_USERS}/me`, { silent: true });
    expect(location.assign).not.toHaveBeenCalled();
  });

  // 用例 ②
  it("匿名启动：V2 探测 401 silent → assign 零调用、v2User=null、v2Ready 落定", async () => {
    const location = stubLocation("/");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.v2Ready).toBe(true));

    expect(result.current.v2User).toBeNull();
    expect(result.current.v1Ready).toBe(true);
    expect(result.current.authReady).toBe(true);
    // V1 探测仅当 localStorage 有 token 才发请求
    expect(getToken).toHaveBeenCalled();
    expect(request).not.toHaveBeenCalled();
    expect(location.assign).not.toHaveBeenCalled();
  });

  // 用例 ③：非 401 失败（500 / 网络错误）→ catch-all 也置 ready（降级未登录 + 告警），不白屏
  it.each([
    ["HTTP 500", () => new V2ApiError("HTTP_500", "请求失败（HTTP 500）", 500)],
    ["网络错误", () => new TypeError("Failed to fetch")],
  ])("V2 探测遇 %s → v2Ready 落定 + console.warn + 降级未登录", async (_label, makeError) => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    requestV2.mockRejectedValueOnce(makeError());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.v2Ready).toBe(true));

    expect(result.current.v2User).toBeNull();
    expect(result.current.authReady).toBe(true);
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });
});

describe("loginV2 / loginV2Mfa 封装契约", () => {
  it("loginV2 成功：snake→camel 映射 + 捕获 csrf_token + 置 v2User", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    requestV2.mockResolvedValueOnce(loginOk());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.v2Ready).toBe(true));

    let outcome;
    await act(async () => {
      outcome = await result.current.loginV2("v2@example.com", "secret");
    });

    expect(requestV2).toHaveBeenLastCalledWith(`${V2_AUTH}/login`, {
      method: "POST",
      body: { email: "v2@example.com", password: "secret" },
    });
    expect(setCsrfToken).toHaveBeenCalledWith("csrf-tok");
    expect(outcome).toEqual({ user: V2_USER_MAPPED });
    expect(result.current.v2User).toEqual(V2_USER_MAPPED);
  });

  it("loginV2 对 mfa_required：返回 {mfaRequired, challengeId}，调用方不见原始键、不置 v2User", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    requestV2.mockResolvedValueOnce(
      loginOk({ mfa_required: true, mfa_challenge_id: "chal-1" })
    );

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.v2Ready).toBe(true));

    let outcome;
    await act(async () => {
      outcome = await result.current.loginV2("v2@example.com", "secret");
    });

    expect(outcome).toEqual({ mfaRequired: true, challengeId: "chal-1" });
    expect(outcome).not.toHaveProperty("mfa_required");
    expect(setCsrfToken).not.toHaveBeenCalled();
    expect(result.current.v2User).toBeNull();
  });

  it("loginV2Mfa：以 mfa_challenge_id/totp_code 提交，成功映射 user + 捕获 csrf", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    requestV2.mockResolvedValueOnce(loginOk());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.v2Ready).toBe(true));

    let outcome;
    await act(async () => {
      outcome = await result.current.loginV2Mfa("chal-1", "123456");
    });

    expect(requestV2).toHaveBeenLastCalledWith(`${V2_AUTH}/login/mfa`, {
      method: "POST",
      body: { mfa_challenge_id: "chal-1", totp_code: "123456" },
    });
    expect(setCsrfToken).toHaveBeenCalledWith("csrf-tok");
    expect(outcome).toEqual({ user: V2_USER_MAPPED });
    expect(result.current.v2User).toEqual(V2_USER_MAPPED);
  });
});

describe("V2 会话过期事件订阅", () => {
  // 用例 ④
  it("401 事件：清 v2User 并 assign /login?v2=1", async () => {
    const location = stubLocation("/");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    requestV2.mockResolvedValueOnce(loginOk());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.v2Ready).toBe(true));
    await act(async () => {
      await result.current.loginV2("v2@example.com", "secret");
    });
    expect(result.current.v2User).toEqual(V2_USER_MAPPED);

    fireSessionExpired();

    expect(result.current.v2User).toBeNull();
    expect(location.assign).toHaveBeenCalledTimes(1);
    expect(location.assign).toHaveBeenCalledWith("/login?v2=1");
  });

  // 用例 ⑤
  it("双会话用户 V2 过期 → assign /login?v2=1；落地 /login 后事件重放不再弹回（pathname 守卫）", async () => {
    let location = stubLocation("/");
    getToken.mockReturnValue("v1-token");
    request.mockResolvedValueOnce({ data: V1_USER });
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：V2 未登录

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));
    expect(result.current.user).toEqual(V1_USER);

    // 补建 V2 会话 → 双会话用户
    requestV2.mockResolvedValueOnce(loginOk());
    await act(async () => {
      await result.current.loginV2("v2@example.com", "secret");
    });
    expect(result.current.v2User).toEqual(V2_USER_MAPPED);

    fireSessionExpired();
    expect(result.current.v2User).toBeNull();
    expect(location.assign).toHaveBeenCalledTimes(1);
    expect(location.assign).toHaveBeenCalledWith("/login?v2=1");

    // 模拟已落地 /login：401 事件重放不触发二次跳转（防同页重载循环）
    location = stubLocation("/login");
    fireSessionExpired();
    expect(location.assign).not.toHaveBeenCalled();
    // V1 会话不受 V2 过期影响
    expect(result.current.user).toEqual(V1_USER);
  });
});

describe("logoutV2 收敛", () => {
  // 用例 ⑥
  it("对 401：Promise resolve 不 reject、无错误上抛、本地 v2User 清空、自身不跳转", async () => {
    const location = stubLocation("/");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    requestV2.mockResolvedValueOnce(loginOk());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.v2Ready).toBe(true));
    await act(async () => {
      await result.current.loginV2("v2@example.com", "secret");
    });

    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    let caught;
    await act(async () => {
      try {
        await result.current.logoutV2();
      } catch (error) {
        caught = error;
      }
    });

    expect(caught).toBeUndefined();
    expect(result.current.v2User).toBeNull();
    expect(location.assign).not.toHaveBeenCalled();
    // 401 时 csrf 清理是 requestV2（真实实现）的职责，logoutV2 自身不再动
    expect(setCsrfToken).not.toHaveBeenCalledWith(null);
  });

  it("对 200：本地登出 + 清内存 csrf", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    requestV2.mockResolvedValueOnce(loginOk());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.v2Ready).toBe(true));
    await act(async () => {
      await result.current.loginV2("v2@example.com", "secret");
    });
    expect(result.current.v2User).toEqual(V2_USER_MAPPED);

    requestV2.mockResolvedValueOnce({ status: 200, data: { ok: true }, headers: new Headers() });
    await act(async () => {
      await result.current.logoutV2();
    });

    expect(result.current.v2User).toBeNull();
    expect(setCsrfToken).toHaveBeenLastCalledWith(null);
  });
});

describe("displayName", () => {
  // 用例 ⑦（四例）
  it("V1 user：username 优先", () => {
    expect(displayName({ username: "alice", email: "alice@example.com" })).toBe("alice");
  });

  it("V2 user：无 username 时取 email 前缀", () => {
    expect(displayName({ email: "v2.user@example.com" })).toBe("v2.user");
  });

  it("无 username 无 email → 空串", () => {
    expect(displayName({})).toBe("");
  });

  it("null / undefined 安全", () => {
    expect(displayName(null)).toBe("");
    expect(displayName(undefined)).toBe("");
  });
});
