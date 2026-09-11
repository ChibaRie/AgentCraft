import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { requestV2, setCsrfToken } from "../api/v2/client.js";
import DeletionCancelPage from "./DeletionCancelPage.jsx";

// refreshV2User 在 AuthContext 层（silent 探测编排 T2/T4 已覆盖）；页面只消费其出口
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn(), setCsrfToken: vi.fn() };
});

const CANCEL_PATH = "/api/v2/account/deletion/cancel";

const refreshV2UserMock = vi.fn();

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定），不依赖假计时器 */
function flush() {
  return act(async () => {});
}

/** entry 为含 query 的路径（/account/deletion/cancel?token=） */
function renderCancel(entry = "/account/deletion/cancel?token=tok-1") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/account/deletion/cancel" element={<DeletionCancelPage />} />
        <Route path="/login" element={<div>登录页落点</div>} />
        <Route path="/" element={<div>工作台落点</div>} />
      </Routes>
    </MemoryRouter>
  );
}

function submitPassword(password = "secret-123") {
  fireEvent.change(screen.getByLabelText("登录密码"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "撤销注销并恢复账户" }));
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({ refreshV2User: refreshV2UserMock });
  refreshV2UserMock.mockResolvedValue(null);
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("提交 → 200 成功链路", () => {
  it("cancel 收到 {cancel_token, password} + 幂等键 → 存 csrf → silent 探测 → 回工作台", async () => {
    refreshV2UserMock.mockResolvedValueOnce({ id: "u-9", status: "active" });
    requestV2.mockResolvedValueOnce({
      status: 200,
      data: {
        user: { id: "u-9", email: "alice@example.com", role: "user", status: "active" },
        csrf_token: "csrf-fresh",
      },
      headers: new Headers(),
    });
    renderCancel();
    submitPassword("secret-123");
    await flush();

    const [path, options] = requestV2.mock.calls.find(([p]) => p === CANCEL_PATH);
    expect(path).toBe(CANCEL_PATH);
    expect(options.method).toBe("POST");
    expect(options.body).toEqual({ cancel_token: "tok-1", password: "secret-123" });
    // 幂等义务端点（A5）：幂等键必带且 ≤100 字符
    expect(options.idempotencyKey).toBeTruthy();
    expect(options.idempotencyKey.length).toBeLessThanOrEqual(100);

    // 200 双 cookie 会话：csrf_token 入内存态 + silent 探测确认会话落定后回工作台
    expect(setCsrfToken).toHaveBeenCalledWith("csrf-fresh");
    expect(refreshV2UserMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("工作台落点")).toBeTruthy();
  });
});

describe("提交校验", () => {
  it("空密码 → 字段级错误，不发请求", async () => {
    renderCancel();
    fireEvent.click(screen.getByRole("button", { name: "撤销注销并恢复账户" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("请输入登录密码");
    expect(requestV2).not.toHaveBeenCalled();
  });
});

describe("失败分流", () => {
  it("409 ACCOUNT_DELETING → 失效页「撤销链接无效或已过期」，无提交按钮", async () => {
    const error = new Error("撤销链接无效或已过期");
    error.name = "V2ApiError";
    error.code = "ACCOUNT_DELETING";
    error.status = 409;
    requestV2.mockRejectedValueOnce(error);
    renderCancel();
    submitPassword();
    await flush();

    expect(screen.getByText("撤销链接无效或已过期")).toBeTruthy();
    expect(screen.queryByLabelText("登录密码")).toBeNull();
    expect(screen.queryByRole("button", { name: "撤销注销并恢复账户" })).toBeNull();
  });

  it("query 无 token → 直接失效页，不发请求", () => {
    renderCancel("/account/deletion/cancel");
    expect(screen.getByText("撤销链接无效或已过期")).toBeTruthy();
    expect(requestV2).not.toHaveBeenCalled();
  });

  it("401 验密失败 → 内联 alert 直显后端文案，停留表单", async () => {
    const error = new Error("当前密码不正确");
    error.name = "V2ApiError";
    error.code = "INVALID_CREDENTIALS";
    error.status = 401;
    requestV2.mockRejectedValueOnce(error);
    renderCancel();
    submitPassword("wrong-pass");
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("当前密码不正确");
    expect(screen.getByLabelText("登录密码")).toBeTruthy();
    // 验密失败不导航：停留表单（回工作台仅发生在 200 链路）
    expect(screen.queryByText("工作台落点")).toBeNull();
  });

  it("429 → Retry-After 秒倒计时禁用提交按钮，归零自动解除", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const error = new Error("请求过于频繁，请稍后再试");
    error.name = "V2ApiError";
    error.code = "RATE_LIMITED";
    error.status = 429;
    error.retryAfter = 30;
    requestV2.mockRejectedValueOnce(error);
    renderCancel();
    submitPassword();
    await act(async () => {});

    expect(screen.getByRole("button", { name: "撤销注销并恢复账户" }).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("30");

    act(() => {
      vi.advanceTimersByTime(30000);
    });
    expect(screen.getByRole("button", { name: "撤销注销并恢复账户" }).disabled).toBe(false);
    expect(screen.queryByRole("status")).toBeNull();
  });
});
