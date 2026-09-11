import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import LoginPage from "./LoginPage.jsx";

// 只 mock useAuth 出口：login/loginV2/loginV2Mfa 由用例编排（网络在 AuthContext 层，T2 已覆盖）
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));

const loginMock = vi.fn();
const loginV2Mock = vi.fn();
const loginV2MfaMock = vi.fn();

const V1_USER = { id: 1, username: "alice", email: "alice@example.com", role: "user" };
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
    user: null,
    v2User: null,
    login: loginMock,
    loginV2: loginV2Mock,
    loginV2Mfa: loginV2MfaMock,
  });
  loginMock.mockRejectedValue(new Error("login：本用例未显式编排"));
  loginV2Mock.mockRejectedValue(new Error("loginV2：本用例未显式编排"));
  loginV2MfaMock.mockRejectedValue(new Error("loginV2Mfa：本用例未显式编排"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("双 Tab 与默认激活", () => {
  it("默认 V1 工作区登录：V1 字段在、V2 字段不在、无注册入口（E5）", () => {
    renderLogin();
    expect(screen.getByRole("button", { name: "工作区登录" }).ariaPressed).toBe("true");
    expect(screen.getByLabelText("用户名或邮箱")).toBeTruthy();
    expect(screen.queryByLabelText("邮箱")).toBeNull();
    expect(screen.queryByRole("button", { name: /注册/ })).toBeNull();
  });

  it("?v2=1 落地默认激活账户登录 Tab（401 事件跳转落点）", () => {
    renderLogin("/login?v2=1");
    expect(screen.getByRole("button", { name: "账户登录" }).ariaPressed).toBe("true");
    expect(screen.getByLabelText("邮箱")).toBeTruthy();
    expect(screen.queryByLabelText("用户名或邮箱")).toBeNull();
  });

  it("双 Tab 切换不保留另一 Tab 表单态（简单性裁决，测试钉死）", () => {
    renderLogin("/login?v2=1");
    fillV2Form();

    fireEvent.click(screen.getByRole("button", { name: "工作区登录" }));
    fireEvent.change(screen.getByLabelText("用户名或邮箱"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "v1-secret" } });

    fireEvent.click(screen.getByRole("button", { name: "账户登录" }));
    expect(screen.getByLabelText("邮箱").value).toBe("");
    expect(screen.getByLabelText("密码").value).toBe("");

    fireEvent.click(screen.getByRole("button", { name: "工作区登录" }));
    expect(screen.getByLabelText("用户名或邮箱").value).toBe("");
    expect(screen.getByLabelText("密码").value).toBe("");
  });
});

describe("V1 工作区登录（逻辑原样搬入）", () => {
  it("login() 成功 → navigate(from || /)", async () => {
    renderLogin({ pathname: "/login", state: { from: "/tasks" } });
    loginMock.mockResolvedValueOnce({ id: 1 });
    fireEvent.change(screen.getByLabelText("用户名或邮箱"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "v1-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(loginMock).toHaveBeenCalledWith("alice", "v1-secret");
    expect(screen.getByText("任务页落点")).toBeTruthy();
  });

  it("V1 error.message → 表单内联展示", async () => {
    renderLogin();
    loginMock.mockRejectedValueOnce(new Error("用户名或密码不正确"));
    fireEvent.change(screen.getByLabelText("用户名或邮箱"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("用户名或密码不正确");
  });
});

describe("/login 不设路由守卫（E11）", () => {
  it("V1 已登录用户访问 /login 不弹回", () => {
    useAuth.mockReturnValue({
      user: V1_USER,
      v2User: null,
      login: loginMock,
      loginV2: loginV2Mock,
      loginV2Mfa: loginV2MfaMock,
    });
    const view = renderLogin();
    expect(screen.getByRole("button", { name: "工作区登录" })).toBeTruthy();
    expect(screen.queryByText("首页落点")).toBeNull();
    view.unmount();
  });

  it("仅 V2 会话用户访问 /login 同样不弹回（双轨 R1：可补建另一轨会话）", () => {
    useAuth.mockReturnValue({
      user: null,
      v2User: V2_USER,
      login: loginMock,
      loginV2: loginV2Mock,
      loginV2Mfa: loginV2MfaMock,
    });
    renderLogin();
    expect(screen.getByRole("button", { name: "工作区登录" })).toBeTruthy();
    expect(screen.queryByText("首页落点")).toBeNull();
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
    fireEvent.click(screen.getByRole("button", { name: "账户登录" }));
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

  it("403 ACCOUNT_SUSPENDED → 全页提示态（Tab 与表单消失）", async () => {
    renderLogin("/login?v2=1");
    loginV2Mock.mockRejectedValueOnce(v2ApiError("ACCOUNT_SUSPENDED", "账户已被停用", 403));
    fillV2Form();
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    await flush();

    expect(screen.getByText("账户已被停用")).toBeTruthy();
    expect(screen.queryByLabelText("邮箱")).toBeNull();
    expect(screen.queryByRole("button", { name: "账户登录" })).toBeNull();
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
