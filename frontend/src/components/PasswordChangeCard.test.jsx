import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import PasswordChangeCard from "./PasswordChangeCard.jsx";

// refreshV2User 在 AuthContext 层（T4 已覆盖）；页面卡片只消费其出口
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

const refreshV2UserMock = vi.fn();

const V2_USER = {
  id: "u-2",
  email: "v2@example.com",
  role: "user",
  status: "active",
  mfaEnabled: false,
};
const V2_USER_MFA = { ...V2_USER, mfaEnabled: true };

/** 真实 V2ApiError 实例（组件以 instanceof 区分网络层裸错误的兜底路径） */
function v2ApiError(code, message, status, retryAfter) {
  return new V2ApiError(code, message, status, retryAfter);
}

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定），不依赖假计时器 */
function flush() {
  return act(async () => {});
}

function fillForm({ current = "old-secret", next = "new-secret", confirm, totp } = {}) {
  fireEvent.change(screen.getByLabelText("当前密码"), { target: { value: current } });
  fireEvent.change(screen.getByLabelText("新密码"), { target: { value: next } });
  fireEvent.change(screen.getByLabelText("确认新密码"), {
    target: { value: confirm ?? next },
  });
  if (totp !== undefined) {
    fireEvent.change(screen.getByLabelText("两步验证码"), { target: { value: totp } });
  }
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({ v2User: V2_USER, refreshV2User: refreshV2UserMock });
  refreshV2UserMock.mockResolvedValue(V2_USER);
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("TOTP 条件显示（mfa_enabled 判据 = users/me / v2User）", () => {
  it("未启用 MFA：不渲染两步验证码；提交 body 无 totp_code 键且不带幂等键（A7 非义务端点）", async () => {
    render(<PasswordChangeCard />);
    expect(screen.queryByLabelText("两步验证码")).toBeNull();

    fillForm();
    requestV2.mockResolvedValueOnce({
      status: 200,
      data: { ok: true },
      headers: new Headers(),
    });
    submit();
    await flush();

    const [path, options] = requestV2.mock.calls[0];
    expect(path).toBe("/api/auth/password-change");
    expect(options.method).toBe("POST");
    expect(options.body).toEqual({
      current_password: "old-secret",
      new_password: "new-secret",
    });
    expect("totp_code" in options.body).toBe(false);
    expect(options.idempotencyKey).toBeUndefined();
  });

  it("已启用 MFA：两步验证码显示且必填；提交 body 携带 totp_code", async () => {
    useAuth.mockReturnValue({ v2User: V2_USER_MFA, refreshV2User: refreshV2UserMock });
    render(<PasswordChangeCard />);
    expect(screen.getByLabelText("两步验证码")).toBeTruthy();

    // 未填验证码：客户端必填门拦截，不发请求
    fillForm({ totp: "" });
    submit();
    await flush();
    expect(requestV2).not.toHaveBeenCalled();
    expect(screen.getByText("请输入 6-8 位两步验证码")).toBeTruthy();

    fillForm({ totp: "654321" });
    requestV2.mockResolvedValueOnce({
      status: 200,
      data: { ok: true },
      headers: new Headers(),
    });
    submit();
    await flush();

    expect(requestV2.mock.calls[0][1].body).toEqual({
      current_password: "old-secret",
      new_password: "new-secret",
      totp_code: "654321",
    });
  });
});

describe("客户端校验门", () => {
  it("空表单 / 两次新密码不一致：内联错误且不发请求", async () => {
    render(<PasswordChangeCard />);

    submit();
    await flush();
    expect(requestV2).not.toHaveBeenCalled();
    expect(screen.getByText("请输入当前密码")).toBeTruthy();
    expect(screen.getByText("请输入新密码")).toBeTruthy();

    fillForm({ confirm: "different" });
    submit();
    await flush();
    expect(requestV2).not.toHaveBeenCalled();
    expect(screen.getByText("两次输入的新密码不一致")).toBeTruthy();
  });

  it("in-flight gate：提交中按钮禁用，落定后解除", async () => {
    let resolveRequest;
    requestV2.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveRequest = resolve;
      })
    );
    render(<PasswordChangeCard />);
    fillForm();
    submit();

    expect(screen.getByRole("button", { name: "提交中…" }).disabled).toBe(true);

    resolveRequest({ status: 200, data: { ok: true }, headers: new Headers() });
    await flush();
    expect(screen.getByRole("button", { name: "修改密码" }).disabled).toBe(false);
  });
});

describe("提交结果分流", () => {
  it("200 → 提示「密码已修改，其他设备已退出登录」+ 刷新 v2User + 清空字段", async () => {
    render(<PasswordChangeCard />);
    fillForm();
    requestV2.mockResolvedValueOnce({
      status: 200,
      data: { ok: true },
      headers: new Headers(),
    });
    submit();
    await flush();

    expect(screen.getByText("密码已修改，其他设备已退出登录")).toBeTruthy();
    expect(refreshV2UserMock).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("当前密码").value).toBe("");
    expect(screen.getByLabelText("新密码").value).toBe("");
    expect(screen.getByLabelText("确认新密码").value).toBe("");
  });

  it("401 INVALID_CREDENTIALS → 内联后端文案「当前密码不正确」", async () => {
    render(<PasswordChangeCard />);
    fillForm();
    requestV2.mockRejectedValueOnce(
      v2ApiError("INVALID_CREDENTIALS", "当前密码不正确", 401)
    );
    submit();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("当前密码不正确");
    expect(refreshV2UserMock).not.toHaveBeenCalled();
  });

  it("400 MFA_INVALID → 内联后端文案「验证码无效」（已启用 MFA）", async () => {
    useAuth.mockReturnValue({ v2User: V2_USER_MFA, refreshV2User: refreshV2UserMock });
    render(<PasswordChangeCard />);
    fillForm({ totp: "000000" });
    requestV2.mockRejectedValueOnce(v2ApiError("MFA_INVALID", "验证码无效", 400));
    submit();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("验证码无效");
  });

  it("网络层裸错误 → 兜底文案（不透传 error.message）", async () => {
    render(<PasswordChangeCard />);
    fillForm();
    requestV2.mockRejectedValueOnce(new Error("network boom"));
    submit();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("修改失败，请稍后重试");
  });

  it("429 password_change_totp → Retry-After 秒倒计时禁用提交，归零自动解除", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    render(<PasswordChangeCard />);
    fillForm();
    requestV2.mockRejectedValueOnce(
      v2ApiError("RATE_LIMITED", "操作过于频繁，请稍后再试", 429, 30)
    );
    submit();
    await flush();

    expect(screen.getByRole("button", { name: "修改密码" }).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("30");

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole("status").textContent).toContain("28");

    act(() => {
      vi.advanceTimersByTime(28000);
    });
    expect(screen.getByRole("button", { name: "修改密码" }).disabled).toBe(false);
    expect(screen.queryByRole("status")).toBeNull();
  });
});
