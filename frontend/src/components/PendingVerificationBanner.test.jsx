import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import { useAuth } from "../auth/AuthContext.jsx";
import PendingVerificationBanner from "./PendingVerificationBanner.jsx";

// 横幅只消费 useAuth 出口；重发走 useResendVerification → requestV2（网络在此打桩）
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

const PENDING_USER = { id: "u-2", email: "v2@example.com", role: "user", status: "pending" };

/** 真实 V2ApiError 实例（hook 按 instanceof 分流网络层裸错误，替身须同构） */
const v2ApiError = (code, message, status, retryAfter) =>
  new V2ApiError(code, message, status, retryAfter);

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定），不依赖假计时器 */
function flush() {
  return act(async () => {});
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({ v2User: null });
  requestV2.mockResolvedValue({ status: 200, data: { ok: true }, headers: new Headers() });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("pending 横幅渲染条件", () => {
  it("v2User.status === pending → 渲染「邮箱尚未验证」+ 重发按钮", () => {
    useAuth.mockReturnValue({ v2User: PENDING_USER });
    render(<PendingVerificationBanner />);

    expect(screen.getByText("邮箱尚未验证")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重发验证邮件" })).toBeTruthy();
  });

  it.each([
    ["active 用户", { ...PENDING_USER, status: "active" }],
    ["无 v2User（匿名）", null],
  ])("v2User 为%s → 不渲染任何内容", (_label, v2User) => {
    useAuth.mockReturnValue({ v2User });
    render(<PendingVerificationBanner />);

    expect(screen.queryByText("邮箱尚未验证")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("重发验证邮件（60s 前端冷却 + 429 倒计时）", () => {
  it("点击重发 → POST email-verification/resend（认证端点自动 CSRF）；按钮进入 60s 冷却并每秒递减、归零解除", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    useAuth.mockReturnValue({ v2User: PENDING_USER });
    render(<PendingVerificationBanner />);

    fireEvent.click(screen.getByRole("button", { name: "重发验证邮件" }));
    await flush();

    expect(requestV2).toHaveBeenCalledTimes(1);
    expect(requestV2).toHaveBeenCalledWith("/api/auth/email-verification/resend", {
      method: "POST",
    });

    const cooling = screen.getByRole("button", { name: /重新发送（60s）/ });
    expect(cooling.disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("验证邮件已重发");

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole("button", { name: /重新发送（58s）/ }).disabled).toBe(true);

    act(() => {
      vi.advanceTimersByTime(58000);
    });
    expect(screen.getByRole("button", { name: "重发验证邮件" }).disabled).toBe(false);
    expect(screen.queryByRole("button", { name: /重新发送/ })).toBeNull();
  });

  it("429（Retry-After=30）→ 倒计时以服务端秒数覆盖前端冷却，无「已重发」提示", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    useAuth.mockReturnValue({ v2User: PENDING_USER });
    requestV2.mockRejectedValueOnce(v2ApiError("RATE_LIMITED", "请求过于频繁，请稍后再试", 429, 30));
    render(<PendingVerificationBanner />);

    fireEvent.click(screen.getByRole("button", { name: "重发验证邮件" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("请求过于频繁，请稍后再试");
    expect(screen.getByRole("button", { name: /重新发送（30s）/ }).disabled).toBe(true);
    expect(screen.queryByRole("status")).toBeNull();

    act(() => {
      vi.advanceTimersByTime(30000);
    });
    expect(screen.getByRole("button", { name: "重发验证邮件" }).disabled).toBe(false);
  });

  it("429 无 Retry-After 头 → 保留 60s 前端冷却（不归零、不发明默认秒数）", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    useAuth.mockReturnValue({ v2User: PENDING_USER });
    requestV2.mockRejectedValueOnce(v2ApiError("RATE_LIMITED", "请求过于频繁，请稍后再试", 429));
    render(<PendingVerificationBanner />);

    fireEvent.click(screen.getByRole("button", { name: "重发验证邮件" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("请求过于频繁，请稍后再试");
    expect(screen.getByRole("button", { name: /重新发送（60s）/ }).disabled).toBe(true);

    act(() => {
      vi.advanceTimersByTime(60000);
    });
    expect(screen.getByRole("button", { name: "重发验证邮件" }).disabled).toBe(false);
  });

  it("非 429 失败 → role=alert 展示 message，60s 冷却继续生效", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    useAuth.mockReturnValue({ v2User: PENDING_USER });
    requestV2.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    render(<PendingVerificationBanner />);

    fireEvent.click(screen.getByRole("button", { name: "重发验证邮件" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("重发失败，请稍后重试");
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("button", { name: /重新发送（60s）/ }).disabled).toBe(true);
  });
});
