import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth, V2_LOGIN_FROM_STORAGE_KEY } from "../auth/AuthContext.jsx";
import LoginPage from "./LoginPage.jsx";

// mock useAuth 出口（loginV2/loginV2Mfa 由用例编排，网络在 AuthContext 层
// 已覆盖），但保留模块真实导出（V2_LOGIN_FROM_STORAGE_KEY 常量被本文件消费）
vi.mock("../auth/AuthContext.jsx", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useAuth: vi.fn() };
});

const loginV2Mock = vi.fn();
const loginV2MfaMock = vi.fn();

const V2_USER = { id: "u-2", email: "v2@example.com", role: "user", status: "active" };

/** V2ApiError 形状的测试替身（code/status/retryAfter 与 v2/client.js 产出对齐） */
function v2ApiError(code, message, status, retryAfter) {
  const error = new Error(message);
  error.name = "V2ApiError";
  error.code = code;
  error.status = status;
  if (retryAfter !== undefined) {
    error.retryAfter = retryAfter;
  }
  return error;
}

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定），不依赖假计时器 */
function flush() {
  return act(async () => {});
}

/** entry 可为字符串（含 query）或 location 对象（带 state.from） */
function renderLogin(entry = "/login") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/tasks" element={<div>任务页落点</div>} />
        <Route path="/profile" element={<div>个人中心落点</div>} />
        <Route path="/" element={<div>首页落点</div>} />
      </Routes>
    </MemoryRouter>
  );
}

function fillV2Form() {
  fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: "v2@example.com" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "secret" } });
}

/** 走到 MFA 挑战卡片（loginV2 返回 mfa_required 之后） */
async function renderAtChallenge() {
  renderLogin("/login?v2=1");
  loginV2Mock.mockResolvedValueOnce({ mfaRequired: true, challengeId: "chal-9" });
  fillV2Form();
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  await flush();
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({
    v2User: null,
    loginV2: loginV2Mock,
    loginV2Mfa: loginV2MfaMock,
  });
  loginV2Mock.mockRejectedValue(new Error("loginV2：本用例未显式编排"));
  loginV2MfaMock.mockRejectedValue(new Error("loginV2Mfa：本用例未显式编排"));
});

afterEach(() => {
  vi.useRealTimers();
  window.sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("单 Tab 会话归一（T14：双 Tab 拆解）", () => {
  it("默认即账户登录：V2 字段在、V1 工作区登录入口不存在、无注册入口（E5）", () => {
    renderLogin();
    expect(screen.getByLabelText("邮箱")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "工作区登录" })).toBeNull();
    expect(screen.queryByRole("button", { name: "账户登录" })).toBeNull();
    expect(screen.queryByLabelText("用户名或邮箱")).toBeNull();
    expect(screen.queryByRole("button", { name: /注册/ })).toBeNull();
  });

  it("忘记密码链接直达 /password-reset（V2 重置流）", () => {
    renderLogin("/login?v2=1");
    const link = screen.getByRole("link", { name: "忘记密码？" });
    expect(link.getAttribute("href")).toBe("/password-reset");
  });
});

describe("/login 不设路由守卫（E11）", () => {
  it("已登录（V2 会话）用户访问 /login 不弹回", () => {
    useAuth.mockReturnValue({
      v2User: V2_USER,
      loginV2: loginV2Mock,
      loginV2Mfa: loginV2MfaMock,
    });
    const view = renderLogin();
    expect(screen.getByLabelText("邮箱")).toBeTruthy();
    expect(screen.queryByText("首页落点")).toBeNull();
    view.unmount();
  });
});

describe("V2 账户登录 + MFA 挑战态机", () => {
  it("mfa_required → 就地切换挑战卡片（OtpInput + 返回重新登录），表单消失", async () => {
    await renderAtChallenge();

    expect(loginV2Mock).toHaveBeenCalledWith("v2@example.com", "secret");
    expect(screen.getByText("两步验证")).toBeTruthy();
    expect(screen.getByLabelText("两步验证码")).toBeTruthy();
    expect(screen.queryByLabelText("邮箱")).toBeNull();
    expect(screen.getByRole("button", { name: "返回重新登录" })).toBeTruthy();
  });

  it("挑战验证成功 → navigate(from ?? /)，loginV2Mfa 收到 challengeId+totp", async () => {
    renderLogin({ pathname: "/login", state: { from: "/tasks" } });
    loginV2Mock.mockResolvedValueOnce({ mfaRequired: true, challengeId: "chal-9" });
    fillV2Form();
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    loginV2MfaMock.mockResolvedValueOnce({ user: V2_USER });
    fireEvent.change(screen.getByLabelText("两步验证码"), { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并登录" }));
    await flush();

    expect(loginV2MfaMock).toHaveBeenCalledWith("chal-9", "654321");
    expect(screen.getByText("任务页落点")).toBeTruthy();
  });

  it("「返回重新登录」重置挑战态机回 V2 表单", async () => {
    await renderAtChallenge();
    fireEvent.click(screen.getByRole("button", { name: "返回重新登录" }));

    expect(screen.getByLabelText("邮箱")).toBeTruthy();
    expect(screen.queryByLabelText("两步验证码")).toBeNull();
    expect(screen.queryByRole("button", { name: "返回重新登录" })).toBeNull();
  });

  it("401 INVALID_CREDENTIALS → 内联 message + 清空密码输入（保留邮箱）", async () => {
    renderLogin("/login?v2=1");
    loginV2Mock.mockRejectedValueOnce(v2ApiError("INVALID_CREDENTIALS", "账号或密码不正确", 401));
    fillV2Form();
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("账号或密码不正确");
    expect(screen.getByLabelText("密码").value).toBe("");
    expect(screen.getByLabelText("邮箱").value).toBe("v2@example.com");
    expect(screen.queryByLabelText("两步验证码")).toBeNull();
  });

  it("401 MFA_INVALID → 内联 message + 清空验证码", async () => {
    await renderAtChallenge();
    loginV2MfaMock.mockRejectedValueOnce(v2ApiError("MFA_INVALID", "验证码无效或已过期", 401));
    fireEvent.change(screen.getByLabelText("两步验证码"), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并登录" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("验证码无效或已过期");
    expect(screen.getByLabelText("两步验证码").value).toBe("");
  });

  it("403 ACCOUNT_SUSPENDED → 全页提示态（表单消失）", async () => {
    renderLogin("/login?v2=1");
    loginV2Mock.mockRejectedValueOnce(v2ApiError("ACCOUNT_SUSPENDED", "账户已被停用", 403));
    fillV2Form();
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(screen.getByText("账户已被停用")).toBeTruthy();
    expect(screen.queryByLabelText("邮箱")).toBeNull();
  });

  it("429 → Retry-After 秒倒计时禁用提交按钮，归零自动解除", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderLogin("/login?v2=1");
    fillV2Form();
    loginV2Mock.mockRejectedValueOnce(
      v2ApiError("RATE_LIMITED", "请求过于频繁，请稍后再试", 429, 30)
    );
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(screen.getByRole("button", { name: "登录" }).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("30");

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole("status").textContent).toContain("28");

    act(() => {
      vi.advanceTimersByTime(28000);
    });
    expect(screen.getByRole("button", { name: "登录" }).disabled).toBe(false);
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("V2 登录成功恢复 401 暂存 from（T11 ⑥ / D13 / I-4 / T11-M2）", () => {
  it("sessionStorage 有合法 from → V2 登录成功 navigate 回原路径，暂存消费即清除", async () => {
    window.sessionStorage.setItem(V2_LOGIN_FROM_STORAGE_KEY, "/tasks");
    renderLogin("/login?v2=1");
    loginV2Mock.mockResolvedValueOnce({ user: V2_USER });
    fillV2Form();
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(screen.getByText("任务页落点")).toBeTruthy();
    expect(window.sessionStorage.getItem(V2_LOGIN_FROM_STORAGE_KEY)).toBeNull();
  });

  it("MFA 挑战完成路径同样恢复 from", async () => {
    window.sessionStorage.setItem(V2_LOGIN_FROM_STORAGE_KEY, "/profile");
    renderLogin("/login?v2=1");
    loginV2Mock.mockResolvedValueOnce({ mfaRequired: true, challengeId: "chal-9" });
    fillV2Form();
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    loginV2MfaMock.mockResolvedValueOnce({ user: V2_USER });
    fireEvent.change(screen.getByLabelText("两步验证码"), { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并登录" }));
    await flush();

    expect(screen.getByText("个人中心落点")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "验证并登录" })).toBeNull();
    expect(window.sessionStorage.getItem(V2_LOGIN_FROM_STORAGE_KEY)).toBeNull();
  });

  it("暂存值为协议相对串 //evil.com（I-4 绕过形态）→ 拒绝恢复，回落 /", async () => {
    window.sessionStorage.setItem(V2_LOGIN_FROM_STORAGE_KEY, "//evil.com");
    renderLogin("/login?v2=1");
    loginV2Mock.mockResolvedValueOnce({ user: V2_USER });
    fillV2Form();
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(screen.getByText("首页落点")).toBeTruthy();
    expect(screen.queryByText("任务页落点")).toBeNull();
  });

  it("无暂存时回落 location.state?.from（RequireAuth 弹回场景不回归）", async () => {
    renderLogin({ pathname: "/login", state: { from: "/tasks" } });
    loginV2Mock.mockResolvedValueOnce({ user: V2_USER });
    fillV2Form();
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(screen.getByText("任务页落点")).toBeTruthy();
  });

  it("state.from 为协议相对串 //evil.com（M2 白名单）→ 拒绝恢复，回落 /", async () => {
    renderLogin({ pathname: "/login", state: { from: "//evil.com" } });
    loginV2Mock.mockResolvedValueOnce({ user: V2_USER });
    fillV2Form();
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(screen.getByText("首页落点")).toBeTruthy();
    expect(screen.queryByText("任务页落点")).toBeNull();
  });
});
