import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { V2_SESSION_EXPIRED_EVENT, V2ApiError, requestV2, setCsrfToken } from "../api/v2/client.js";
import { V2_AUTH, V2_USERS } from "../api/v2/routes.js";
import { AuthProvider, useAuth, V2_LOGIN_FROM_STORAGE_KEY } from "./AuthContext.jsx";
import { displayName } from "./displayName.js";

// 只 mock 网络入口 requestV2 与 csrf 写口；V2ApiError / V2_SESSION_EXPIRED_EVENT 用真实导出
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn(), setCsrfToken: vi.fn() };
});

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

function meOk(data) {
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
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
  stubLocation("/");
});

afterEach(() => {
  window.sessionStorage.clear();
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("启动探测与会话归一", () => {
  // 用例 ①
  it("挂载即清理 V1 遗留 token（安全审查 I-5：agentcraft_token 残留清零）", async () => {
    localStorage.setItem("agentcraft_token", "stale-v1-token");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名

    renderUseAuth();
    await waitFor(() => expect(vi.mocked(requestV2).mock.calls.length).toBeGreaterThan(0));

    expect(localStorage.getItem("agentcraft_token")).toBeNull();
  });

  // 用例 ②
  it("匿名启动：V2 探测 401 silent → assign 零调用、v2User=null、authReady 落定", async () => {
    const location = stubLocation("/");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    expect(result.current.v2User).toBeNull();
    expect(result.current.authReady).toBe(true);
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
    await waitFor(() => expect(result.current.authReady).toBe(true));

    expect(result.current.v2User).toBeNull();
    expect(result.current.authReady).toBe(true);
    expect(warnSpy).toHaveBeenCalledTimes(1);
  });

  it("V1 轨出口已随 cutover 删除：上下文不再暴露 user/isReady/login/register/applyExpert", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    expect(result.current.user).toBeUndefined();
    expect(result.current.isReady).toBeUndefined();
    expect(result.current.login).toBeUndefined();
    expect(result.current.register).toBeUndefined();
    expect(result.current.applyExpert).toBeUndefined();
    expect(result.current.logout).toBeUndefined();
  });
});

describe("loginV2 / loginV2Mfa 封装契约", () => {
  it("loginV2 成功：snake→camel 映射 + 捕获 csrf_token + 登录后静默 users/me 刷新（T11-M1）", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名
    requestV2.mockResolvedValueOnce(loginOk()); // 登录
    requestV2.mockResolvedValueOnce(
      meOk({ ...V2_USER_RAW, entitlements: ["expert_author"] }) // 登录后刷新
    );

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    let outcome;
    await act(async () => {
      outcome = await result.current.loginV2("v2@example.com", "secret");
    });

    expect(requestV2).toHaveBeenNthCalledWith(2, `${V2_AUTH}/login`, {
      method: "POST",
      body: { email: "v2@example.com", password: "secret" },
    });
    // M1：登录信封无 entitlements → 静默 users/me 刷新补全
    expect(requestV2).toHaveBeenLastCalledWith(`${V2_USERS}/me`, { silent: true });
    expect(setCsrfToken).toHaveBeenCalledWith("csrf-tok");
    expect(outcome).toEqual({ user: V2_USER_MAPPED });
    // 刷新载荷（含 entitlements）置位 → 刚登录的专家立即点亮
    expect(result.current.v2User.entitlements).toEqual(["expert_author"]);
    expect(result.current.isExpert).toBe(true);
  });

  it("loginV2 刷新失败（401/网络）→ 静默收敛，保持登录信封用户（不清会话）", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名
    requestV2.mockResolvedValueOnce(loginOk()); // 登录
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 刷新失败

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    await act(async () => {
      await result.current.loginV2("v2@example.com", "secret");
    });

    expect(result.current.v2User).toEqual(V2_USER_MAPPED);
    expect(result.current.isExpert).toBe(false);
  });

  it("loginV2 对 mfa_required：返回 {mfaRequired, challengeId}，不置 v2User、无刷新（会话未建立）", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名
    requestV2.mockResolvedValueOnce(loginOk({ mfa_required: true, mfa_challenge_id: "chal-1" }));

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    let outcome;
    await act(async () => {
      outcome = await result.current.loginV2("v2@example.com", "secret");
    });

    expect(outcome).toEqual({ mfaRequired: true, challengeId: "chal-1" });
    expect(outcome).not.toHaveProperty("mfa_required");
    expect(setCsrfToken).not.toHaveBeenCalled();
    expect(result.current.v2User).toBeNull();
    // 仅启动探测 + login 两次调用，无第三个 users/me 刷新
    expect(requestV2).toHaveBeenCalledTimes(2);
  });

  it("loginV2Mfa：以 mfa_challenge_id/totp_code 提交，成功映射 user + csrf + 刷新补全", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名
    requestV2.mockResolvedValueOnce(loginOk()); // MFA 挑战成功建会话
    requestV2.mockResolvedValueOnce(
      meOk({ ...V2_USER_RAW, entitlements: ["expert_author"] }) // 登录后刷新
    );

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    let outcome;
    await act(async () => {
      outcome = await result.current.loginV2Mfa("chal-1", "123456");
    });

    expect(requestV2).toHaveBeenNthCalledWith(2, `${V2_AUTH}/login/mfa`, {
      method: "POST",
      body: { mfa_challenge_id: "chal-1", totp_code: "123456" },
    });
    expect(setCsrfToken).toHaveBeenCalledWith("csrf-tok");
    expect(outcome).toEqual({ user: V2_USER_MAPPED });
    expect(result.current.isExpert).toBe(true);
  });
});

describe("V2 会话过期事件订阅", () => {
  // 用例 ④
  it("401 事件：清 v2User 并 assign /login?v2=1", async () => {
    const location = stubLocation("/");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名
    requestV2.mockResolvedValueOnce(loginOk()); // 登录
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 登录后刷新失败（静默）

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));
    await act(async () => {
      await result.current.loginV2("v2@example.com", "secret");
    });
    expect(result.current.v2User).toEqual(V2_USER_MAPPED);

    fireSessionExpired();

    expect(result.current.v2User).toBeNull();
    expect(location.assign).toHaveBeenCalledTimes(1);
    expect(location.assign).toHaveBeenCalledWith("/login?v2=1");
  });

  it("已在 /login：事件重放不再弹回（pathname 守卫）", async () => {
    stubLocation("/login");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    fireSessionExpired();

    expect(result.current.v2User).toBeNull();
  });
});

describe("logoutV2 收敛", () => {
  // 用例 ⑥
  it("对 401：Promise resolve 不 reject、无错误上抛、本地 v2User 清空、自身不跳转", async () => {
    const location = stubLocation("/");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名
    requestV2.mockResolvedValueOnce(loginOk()); // 登录
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 登录后刷新失败（静默）

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));
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
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名
    requestV2.mockResolvedValueOnce(loginOk()); // 登录
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 登录后刷新失败（静默）

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));
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

describe("clearV2Session 本地清理（FE-T7）", () => {
  it("零网络请求、零事件派发：清内存 csrf + 置空 v2User", async () => {
    const location = stubLocation("/");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名
    requestV2.mockResolvedValueOnce(loginOk()); // 登录
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 登录后刷新失败（静默）

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));
    await act(async () => {
      await result.current.loginV2("v2@example.com", "secret");
    });
    expect(result.current.v2User).toEqual(V2_USER_MAPPED);
    setCsrfToken.mockClear();

    act(() => {
      result.current.clearV2Session();
    });

    expect(result.current.v2User).toBeNull();
    expect(setCsrfToken).toHaveBeenCalledTimes(1);
    expect(setCsrfToken).toHaveBeenCalledWith(null);
    // 清理本身零请求（探测 + 登录 + 刷新共 3 次）；注销受理后的冻结页不被 401 事件打断
    expect(requestV2).toHaveBeenCalledTimes(3);
    expect(location.assign).not.toHaveBeenCalled();
  });
});

describe("acceptInvitation / refreshV2User（FE-T4）", () => {
  // invitations/accept 信封：{csrf_token}（会话种入）；users/me 信封：user 原始形状
  const PENDING_RAW = { id: "u-9", email: "new@example.com", role: "user", status: "pending" };

  function acceptOk(data = { csrf_token: "csrf-tok" }) {
    return { status: 200, data, headers: new Headers() };
  }

  it("accept 200 + 探测 200：带 Idempotency-Key 提交、存 csrf、探测置位 v2User(pending)", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401()); // 启动探测：匿名
    requestV2.mockResolvedValueOnce(acceptOk());
    requestV2.mockResolvedValueOnce({ status: 200, data: PENDING_RAW, headers: new Headers() });

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    let outcome;
    await act(async () => {
      outcome = await result.current.acceptInvitation("new@example.com", "secret", "tok-1");
    });

    const [acceptPath, acceptOptions] = requestV2.mock.calls[1];
    expect(acceptPath).toBe(`${V2_AUTH}/invitations/accept`);
    expect(acceptOptions.method).toBe("POST");
    expect(acceptOptions.body).toEqual({
      invitation_token: "tok-1",
      email: "new@example.com",
      password: "secret",
    });
    expect(acceptOptions.idempotencyKey).toBeTruthy();
    expect(acceptOptions.idempotencyKey.length).toBeLessThanOrEqual(100);
    expect(setCsrfToken).toHaveBeenCalledWith("csrf-tok");
    expect(requestV2).toHaveBeenLastCalledWith(`${V2_USERS}/me`, { silent: true });
    expect(outcome).toEqual({ sessionConfirmed: true });
    expect(result.current.v2User).toEqual(PENDING_RAW);
  });

  it("accept 200 + 探测 401（重放/竞态会话未落）→ sessionConfirmed=false、不置 v2User、csrf 已存", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    requestV2.mockResolvedValueOnce(acceptOk());
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    let outcome;
    await act(async () => {
      outcome = await result.current.acceptInvitation("new@example.com", "secret", "tok-1");
    });

    expect(outcome).toEqual({ sessionConfirmed: false });
    expect(result.current.v2User).toBeNull();
    expect(setCsrfToken).toHaveBeenCalledWith("csrf-tok");
  });

  it("accept 409 INVITATION_INVALID → 原样抛 V2ApiError、不发探测、不存 csrf", async () => {
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    requestV2.mockRejectedValueOnce(
      new V2ApiError("INVITATION_INVALID", "邀请链接无效或已失效", 409)
    );

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    let caught;
    await act(async () => {
      try {
        await result.current.acceptInvitation("new@example.com", "secret", "tok-1");
      } catch (error) {
        caught = error;
      }
    });

    expect(caught).toBeInstanceOf(V2ApiError);
    expect(caught.code).toBe("INVITATION_INVALID");
    expect(caught.status).toBe(409);
    expect(setCsrfToken).not.toHaveBeenCalled();
    // 仅启动探测 + accept 两次调用，无第三个 users/me
    expect(requestV2).toHaveBeenCalledTimes(2);
  });

  it("refreshV2User：探测 200 → 置位（pending→active）；探测失败 → 清空并返回 null", async () => {
    requestV2.mockResolvedValueOnce({ status: 200, data: PENDING_RAW, headers: new Headers() }); // 启动探测：pending 会话

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));
    expect(result.current.v2User.status).toBe("pending");

    requestV2.mockResolvedValueOnce({
      status: 200,
      data: { ...PENDING_RAW, status: "active" },
      headers: new Headers(),
    });
    let refreshed;
    await act(async () => {
      refreshed = await result.current.refreshV2User();
    });
    expect(refreshed).toEqual({ ...PENDING_RAW, status: "active" });
    expect(result.current.v2User.status).toBe("active");

    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());
    let second;
    await act(async () => {
      second = await result.current.refreshV2User();
    });
    expect(second).toBeNull();
    expect(result.current.v2User).toBeNull();
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

describe("isExpert 单判据（T14 收敛：V2 entitlements 含 expert_author）", () => {
  it("启动探测 entitlements 含 expert_author → isExpert true", async () => {
    requestV2.mockResolvedValueOnce(
      meOk({ ...V2_USER_RAW, entitlements: ["expert_author"] })
    );

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    expect(result.current.isExpert).toBe(true);
  });

  it("entitlements 空 → isExpert false", async () => {
    requestV2.mockResolvedValueOnce(meOk({ ...V2_USER_RAW, entitlements: [] }));

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    expect(result.current.isExpert).toBe(false);
  });

  it("users/me 载荷无 entitlements 键（登录信封形态）→ isExpert false 不误判", async () => {
    requestV2.mockResolvedValueOnce(meOk(V2_USER_RAW));

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    expect(result.current.isExpert).toBe(false);
  });
});

describe("401 会话过期 from 暂存（T11 ⑥ / D13 / 安全审查 I-4）", () => {
  it("非 /login 页 401：sessionStorage 暂存 pathname + assign /login?v2=1", async () => {
    const location = stubLocation("/profile");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    fireSessionExpired();

    expect(window.sessionStorage.getItem(V2_LOGIN_FROM_STORAGE_KEY)).toBe("/profile");
    expect(location.assign).toHaveBeenCalledWith("/login?v2=1");
  });

  it("pathname 为协议相对串 //evil.com：白名单拒绝暂存，跳转仍收敛 /login", async () => {
    const location = stubLocation("//evil.com");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    fireSessionExpired();

    expect(window.sessionStorage.getItem(V2_LOGIN_FROM_STORAGE_KEY)).toBeNull();
    expect(location.assign).toHaveBeenCalledWith("/login?v2=1");
  });

  it("已在 /login：pathname 守卫既不跳转也不暂存", async () => {
    const location = stubLocation("/login");
    requestV2.mockRejectedValueOnce(SESSION_EXPIRED_401());

    const { result } = renderUseAuth();
    await waitFor(() => expect(result.current.authReady).toBe(true));

    fireSessionExpired();

    expect(location.assign).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(V2_LOGIN_FROM_STORAGE_KEY)).toBeNull();
  });
});
