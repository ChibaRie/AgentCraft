import { StrictMode, useEffect } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import { AuthProvider, useAuth } from "../auth/AuthContext.jsx";
import EmailVerificationPage from "./EmailVerificationPage.jsx";

// 真实 AuthProvider（probe→confirm→refresh 的编排契约整体受测）；仅网络入口打桩。
// 默认拒绝 401 SESSION_EXPIRED：silent 探测语义下的「未登录」，不触发 console.warn。
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn(), setCsrfToken: vi.fn() };
});

const CONFIRM_PATH = "/api/v2/auth/email-verification/confirm";
const ME_PATH = "/api/v2/users/me";

const PENDING_RAW = { id: "u-9", email: "pending@example.com", role: "user", status: "pending" };

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

function meError() {
  return new V2ApiError("SESSION_EXPIRED", "登录状态已失效，请重新登录", 401);
}

/**
 * 记录 v2User 状态演化的探针（挂在 Router 内、页面旁）。
 * contextStates：每次渲染后的 v2User 快照序列。
 */
function renderVerification(entry = "/email-verification?token=tok-1", { strict = false } = {}) {
  const contextStates = [];
  function ContextSpy() {
    const { v2User } = useAuth();
    useEffect(() => {
      contextStates.push(v2User);
    });
    return null;
  }
  const tree = (
    <AuthProvider>
      <MemoryRouter initialEntries={[entry]}>
        <ContextSpy />
        <Routes>
          <Route path="/email-verification" element={<EmailVerificationPage />} />
          <Route path="/login" element={<div>登录页落点</div>} />
          <Route path="/" element={<div>工作台落点</div>} />
        </Routes>
      </MemoryRouter>
    </AuthProvider>
  );
  render(strict ? <StrictMode>{tree}</StrictMode> : tree);
  return { contextStates };
}

function confirmCalls() {
  return requestV2.mock.calls.filter(([path]) => path === CONFIRM_PATH);
}

beforeEach(() => {
  vi.resetAllMocks();
  requestV2.mockRejectedValue(meError());
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("pending 会话验证流（挂载即 confirm → 200 → 状态 active，守卫不弹回）", () => {
  it("启动探测 pending → confirm 200 → refresh 探测 active；成功页带「返回工作台」并落回工作台", async () => {
    requestV2.mockResolvedValueOnce(ok(PENDING_RAW)); // 启动探测：pending 会话
    requestV2.mockResolvedValueOnce(ok({ ok: true })); // confirm
    requestV2.mockResolvedValueOnce(ok({ ...PENDING_RAW, status: "active" })); // refresh 探测
    const { contextStates } = renderVerification();

    await waitFor(() => expect(screen.getByText("邮箱验证成功")).toBeTruthy());

    // 挂载即自动提交 confirm（无用户交互）
    const confirms = confirmCalls();
    expect(confirms).toHaveLength(1);
    const [, confirmOptions] = confirms[0];
    expect(confirmOptions.method).toBe("POST");
    expect(confirmOptions.body).toEqual({ token: "tok-1" });
    expect(confirmOptions.idempotencyKey).toBeTruthy();
    expect(confirmOptions.idempotencyKey.length).toBeLessThanOrEqual(100);

    // 状态 active（守卫不弹回的语义前提）：refresh 探测置位 active
    await waitFor(() => {
      const last = contextStates[contextStates.length - 1];
      expect(last?.status).toBe("active");
    });
    expect(requestV2).toHaveBeenLastCalledWith(ME_PATH, { silent: true });

    // 已登录 → 「返回工作台」入口
    fireEvent.click(screen.getByRole("button", { name: "返回工作台" }));
    await waitFor(() => expect(screen.getByText("工作台落点")).toBeTruthy());
  });
});

describe("匿名访问（无 V2 会话）", () => {
  it("confirm 200 → 成功页显「前往登录」链接（/login?v2=1），无「返回工作台」", async () => {
    requestV2.mockResolvedValueOnce(ok({ ok: true })); // confirm（启动探测 401 走默认拒绝）
    renderVerification();

    await waitFor(() => expect(screen.getByText("邮箱验证成功")).toBeTruthy());

    const loginLink = screen.getByRole("link", { name: "前往登录" });
    expect(loginLink.getAttribute("href")).toBe("/login?v2=1");
    expect(screen.queryByRole("button", { name: "返回工作台" })).toBeNull();
    // 启动探测（401）+ confirm + confirm 后统一刷新探测（401 → 维持未登录）
    expect(requestV2).toHaveBeenCalledTimes(3);
    expect(requestV2).toHaveBeenLastCalledWith(ME_PATH, { silent: true });
  });
});

describe("confirm 失败分流", () => {
  it("400 EMAIL_NOT_VERIFIED → 失效页（链接无效或已过期）", async () => {
    requestV2.mockRejectedValueOnce(
      new V2ApiError("EMAIL_NOT_VERIFIED", "验证链接无效或已过期", 400)
    );
    renderVerification();

    await waitFor(() => expect(screen.getByText("验证链接无效或已失效")).toBeTruthy());
    expect(screen.queryByText("邮箱验证成功")).toBeNull();
    expect(screen.getByRole("link", { name: "前往登录" })).toBeTruthy();
  });

  it("其它失败（500）→ 失败页展示 message +「重新尝试」复用同一幂等键重发", async () => {
    requestV2.mockRejectedValueOnce(new V2ApiError("HTTP_500", "请求失败（HTTP 500）", 500));
    renderVerification();

    await waitFor(() => expect(screen.getByText("验证失败")).toBeTruthy());
    expect(screen.getByRole("alert").textContent).toBe("请求失败（HTTP 500）");

    requestV2.mockResolvedValueOnce(ok({ ok: true }));
    fireEvent.click(screen.getByRole("button", { name: "重新尝试" }));
    await waitFor(() => expect(screen.getByText("邮箱验证成功")).toBeTruthy());

    const confirms = confirmCalls();
    expect(confirms).toHaveLength(2);
    const keys = confirms.map(([, options]) => options.idempotencyKey);
    // 幂等义务端点：重试必须复用同一把 Idempotency-Key（服务端可安全重放）
    expect(keys[0]).toBeTruthy();
    expect(keys[0]).toBe(keys[1]);
  });
});

describe("React.StrictMode 双执行陷阱", () => {
  it("双跑 effect 下 confirm 仅发一次（useRef 初始化器幂等键 + submitted 闸门）", async () => {
    requestV2.mockResolvedValueOnce(ok({ ok: true })); // confirm
    renderVerification("/email-verification?token=tok-1", { strict: true });

    await waitFor(() => expect(screen.getByText("邮箱验证成功")).toBeTruthy());

    // 启动探测双跑（2）+ confirm 恰好一次（submitted 闸门）+ confirm 后刷新探测（1）
    const confirms = confirmCalls();
    expect(confirms).toHaveLength(1);
    expect(confirmCalls()[0][1].idempotencyKey).toBeTruthy();
    expect(requestV2).toHaveBeenCalledTimes(4);
  });
});
