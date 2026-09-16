import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import MfaCard from "./MfaCard.jsx";

vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

const SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP";
const OTPAUTH_URI = `otpauth://totp/AgentCraft:v2@example.com?secret=${SECRET}&issuer=AgentCraft`;

const V2_USER_OFF = {
  id: "u-2",
  email: "v2@example.com",
  role: "user",
  status: "active",
  mfaEnabled: false,
};
const V2_USER_ON = { ...V2_USER_OFF, mfaEnabled: true };
const V2_ADMIN_ON = { ...V2_USER_ON, role: "admin" };

/** 真实 V2ApiError 实例（组件以 instanceof 区分网络层裸错误的兜底路径） */
function v2ApiError(code, message, status, retryAfter) {
  return new V2ApiError(code, message, status, retryAfter);
}

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定），不依赖假计时器 */
function flush() {
  return act(async () => {});
}

/**
 * 带状态的渲染 Harness：refreshV2User 按 Promise 返回值回写 v2User 并强制
 * 重渲染——复现生产路径（AuthContext.refreshV2User → setV2User → 上下文翻转），
 * 卡片「启用/停用」视图切换由真实数据流驱动，不引入本地覆盖态。
 */
function renderMfaCard(initialUser) {
  let v2User = initialUser;
  const refreshV2UserMock = vi.fn();

  function Harness() {
    const [, force] = useState(0);
    useAuth.mockReturnValue({
      v2User,
      refreshV2User: async () => {
        const next = await refreshV2UserMock();
        if (next) {
          v2User = next;
          force((n) => n + 1);
        }
        return next;
      },
    });
    return <MfaCard />;
  }

  render(<Harness />);
  return { refreshV2UserMock };
}

/** 走到 setup 面板（展示密钥复制块 + OtpInput）；返回 harness 供后续编排 refresh */
async function openSetup() {
  const harness = renderMfaCard(V2_USER_OFF);
  // 响应须先于点击编排：handler 在点击事件内同步发出 setup 请求
  requestV2.mockResolvedValueOnce({
    status: 200,
    data: { secret: SECRET, otpauth_uri: OTPAUTH_URI },
    headers: new Headers(),
  });
  fireEvent.click(screen.getByRole("button", { name: "开始设置" }));
  await flush();
  return harness;
}

beforeEach(() => {
  vi.resetAllMocks();
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("mfa_enabled 分支（判据 = users/me / v2User）", () => {
  it("未启用：渲染「开始设置」，无验证码输入、无停用按钮，挂载不发请求", () => {
    renderMfaCard(V2_USER_OFF);

    expect(screen.getByRole("button", { name: "开始设置" })).toBeTruthy();
    expect(screen.queryByLabelText("两步验证码")).toBeNull();
    expect(screen.queryByRole("button", { name: "停用" })).toBeNull();
    expect(requestV2).not.toHaveBeenCalled();
  });

  it("已启用：渲染「已启用两步验证」与「停用」按钮，无设置入口", () => {
    renderMfaCard(V2_USER_ON);

    expect(screen.getByText("已启用两步验证")).toBeTruthy();
    expect(screen.getByRole("button", { name: "停用" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "开始设置" })).toBeNull();
    expect(requestV2).not.toHaveBeenCalled();
  });
});

describe("设置流（setup → 复制 → activate）", () => {
  it("开始设置 → POST /auth/mfa/setup（无请求体）→ 展示 base32 全文 + OtpInput", async () => {
    await openSetup();

    expect(requestV2).toHaveBeenCalledTimes(1);
    const [path, options] = requestV2.mock.calls[0];
    expect(path).toBe("/api/auth/mfa/setup");
    expect(options.method).toBe("POST");
    expect(options.body).toBeUndefined();

    expect(screen.getByText(SECRET)).toBeTruthy();
    expect(screen.getByLabelText("两步验证码")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "开始设置" })).toBeNull();
  });

  it("复制按钮：navigator.clipboard 收到密钥 / otpauth 全文，按钮反馈「已复制」", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    await openSetup();

    const secretButton = screen.getByRole("button", { name: "复制密钥" });
    fireEvent.click(secretButton);
    await flush();
    expect(writeText).toHaveBeenLastCalledWith(SECRET);
    expect(screen.getByRole("button", { name: "已复制" })).toBeTruthy();

    const uriButton = screen.getByRole("button", { name: "复制链接" });
    fireEvent.click(uriButton);
    await flush();
    expect(writeText).toHaveBeenLastCalledWith(OTPAUTH_URI);
    expect(screen.getAllByRole("button", { name: "已复制" }).length).toBe(2);
  });

  it("clipboard 不可用 → 按钮反馈「复制失败」，不抛未捕获异常", async () => {
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: vi.fn().mockRejectedValue(new Error("denied")),
      },
      configurable: true,
    });
    await openSetup();

    fireEvent.click(screen.getByRole("button", { name: "复制密钥" }));
    await flush();
    expect(screen.getByRole("button", { name: "复制失败" })).toBeTruthy();
  });

  it("activate 200 → refreshV2User 刷新后卡片翻转到「已启用」", async () => {
    const { refreshV2UserMock } = await openSetup();

    fireEvent.change(screen.getByLabelText("两步验证码"), {
      target: { value: "123456" },
    });
    requestV2.mockResolvedValueOnce({
      status: 200,
      data: { mfa_enabled: true },
      headers: new Headers(),
    });
    refreshV2UserMock.mockResolvedValueOnce({ ...V2_USER_OFF, mfaEnabled: true });
    fireEvent.click(screen.getByRole("button", { name: "确认启用" }));
    await flush();

    expect(requestV2.mock.calls[1]).toEqual([
      "/api/auth/mfa/activate",
      { method: "POST", body: { totp_code: "123456" } },
    ]);
    expect(refreshV2UserMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("已启用两步验证")).toBeTruthy();
    expect(screen.queryByLabelText("两步验证码")).toBeNull();
  });

  it("activate 400 MFA_INVALID → 内联后端文案 + 清空验证码", async () => {
    await openSetup();
    fireEvent.change(screen.getByLabelText("两步验证码"), {
      target: { value: "000000" },
    });
    requestV2.mockRejectedValueOnce(v2ApiError("MFA_INVALID", "验证码无效或已过期", 400));
    fireEvent.click(screen.getByRole("button", { name: "确认启用" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("验证码无效或已过期");
    expect(screen.getByLabelText("两步验证码").value).toBe("");
    // 未翻转：仍在设置面板（OtpInput 在、停用按钮不在）
    expect(screen.getByLabelText("两步验证码")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "停用" })).toBeNull();
  });

  it("activate 429 → Retry-After 秒倒计时禁用提交，归零自动解除", async () => {
    await openSetup();
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    fireEvent.change(screen.getByLabelText("两步验证码"), {
      target: { value: "123456" },
    });
    requestV2.mockRejectedValueOnce(
      v2ApiError("RATE_LIMITED", "操作过于频繁，请稍后再试", 429, 30)
    );
    fireEvent.click(screen.getByRole("button", { name: "确认启用" }));
    await flush();

    expect(screen.getByRole("button", { name: "确认启用" }).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("30");

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole("status").textContent).toContain("28");

    act(() => {
      vi.advanceTimersByTime(28000);
    });
    expect(screen.getByRole("button", { name: "确认启用" }).disabled).toBe(false);
  });
});

describe("停用流（admin 置灰 + 403 兜底）", () => {
  it("admin：停用按钮置灰 + title「管理员不可停用两步验证」，点击不发请求", async () => {
    renderMfaCard(V2_ADMIN_ON);

    const button = screen.getByRole("button", { name: "停用" });
    expect(button.disabled).toBe(true);
    expect(button.title).toBe("管理员不可停用两步验证");

    fireEvent.click(button);
    await flush();
    expect(requestV2).not.toHaveBeenCalled();
  });

  it("非 admin：停用 → DELETE /auth/mfa → 200 → refreshV2User 翻转回未启用", async () => {
    const { refreshV2UserMock } = renderMfaCard(V2_USER_ON);
    requestV2.mockResolvedValueOnce({
      status: 200,
      data: { mfa_enabled: false },
      headers: new Headers(),
    });
    refreshV2UserMock.mockResolvedValueOnce({ ...V2_USER_ON, mfaEnabled: false });

    fireEvent.click(screen.getByRole("button", { name: "停用" }));
    await flush();

    expect(requestV2.mock.calls[0]).toEqual(["/api/auth/mfa", { method: "DELETE" }]);
    expect(refreshV2UserMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "开始设置" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "停用" })).toBeNull();
  });

  it("停用 403 FORBIDDEN（兜底）→ 内联后端文案，保持已启用态", async () => {
    renderMfaCard(V2_USER_ON);
    requestV2.mockRejectedValueOnce(v2ApiError("FORBIDDEN", "管理员不可停用 TOTP", 403));

    fireEvent.click(screen.getByRole("button", { name: "停用" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("管理员不可停用 TOTP");
    expect(screen.getByRole("button", { name: "停用" })).toBeTruthy();
  });
});
