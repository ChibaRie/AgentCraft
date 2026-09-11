import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { requestV2 } from "../api/v2/client.js";
import PasswordResetRequestPage from "./PasswordResetRequestPage.jsx";

// 仅网络入口打桩；页面无 AuthContext 依赖（公开端点，无会话语义）
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

const REQUEST_PATH = "/api/v2/auth/password-reset/request";

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定），不依赖假计时器 */
function flush() {
  return act(async () => {});
}

function renderRequest() {
  return render(
    <MemoryRouter initialEntries={["/password-reset"]}>
      <Routes>
        <Route path="/password-reset" element={<PasswordResetRequestPage />} />
        <Route path="/login" element={<div>登录页落点</div>} />
      </Routes>
    </MemoryRouter>
  );
}

function submitEmail(email = "alice@example.com") {
  fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: email } });
  fireEvent.click(screen.getByRole("button", { name: "发送重置链接" }));
}

beforeEach(() => {
  vi.resetAllMocks();
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("提交 → 202 防枚举恒定文案", () => {
  it("202 → 恒展示「如该邮箱存在，重置链接已发送」+ 返回登录链接；请求体只含 email（无 CSRF 无幂等义务）", async () => {
    requestV2.mockResolvedValueOnce({
      status: 202,
      data: { accepted: true },
      headers: new Headers(),
    });
    renderRequest();
    submitEmail("  alice@example.com  ");
    await flush();

    // 202 防枚举语义：文案恒定，不得区分存在性（不出现「不存在/未注册」类字样）
    expect(screen.getByText("如该邮箱存在，重置链接已发送")).toBeTruthy();
    expect(screen.queryByText(/不存在/)).toBeNull();
    expect(screen.queryByText(/未注册/)).toBeNull();
    expect(screen.queryByLabelText("邮箱")).toBeNull();

    const [path, options] = requestV2.mock.calls[0];
    expect(path).toBe(REQUEST_PATH);
    expect(options.method).toBe("POST");
    expect(options.body).toEqual({ email: "alice@example.com" });
    // 公开端点：无幂等键（A5 未列入键控）；email trim 后提交
    expect(options.idempotencyKey).toBeUndefined();

    const loginLink = screen.getByRole("link", { name: "返回登录" });
    expect(loginLink.getAttribute("href")).toBe("/login?v2=1");
  });
});

describe("提交校验", () => {
  it("空邮箱 → 字段级错误，不发请求", async () => {
    renderRequest();
    fireEvent.click(screen.getByRole("button", { name: "发送重置链接" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("请输入邮箱");
    expect(requestV2).not.toHaveBeenCalled();
  });
});

describe("错误分流", () => {
  it("429 → Retry-After 秒倒计时禁用提交按钮，归零自动解除", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const error = new Error("请求过于频繁，请稍后再试");
    error.name = "V2ApiError";
    error.code = "RATE_LIMITED";
    error.status = 429;
    error.retryAfter = 30;
    requestV2.mockRejectedValueOnce(error);
    renderRequest();
    submitEmail();
    await act(async () => {});

    expect(screen.getByRole("button", { name: "发送重置链接" }).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("30");

    act(() => {
      vi.advanceTimersByTime(30000);
    });
    expect(screen.getByRole("button", { name: "发送重置链接" }).disabled).toBe(false);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("其它错误（如 500）→ 表单内联 alert 直显后端中文文案，停留表单", async () => {
    const error = new Error("服务暂时不可用，请稍后重试");
    error.name = "V2ApiError";
    error.code = "HTTP_500";
    error.status = 500;
    requestV2.mockRejectedValueOnce(error);
    renderRequest();
    submitEmail();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("服务暂时不可用，请稍后重试");
    expect(screen.getByLabelText("邮箱")).toBeTruthy();
  });
});
