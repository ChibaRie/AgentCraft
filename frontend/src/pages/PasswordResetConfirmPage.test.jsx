import { StrictMode } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { requestV2 } from "../api/v2/client.js";
import PasswordResetConfirmPage from "./PasswordResetConfirmPage.jsx";

// 页面无 AuthContext 依赖（confirm 为公开令牌端点，无会话语义）；仅网络入口打桩
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

const CONFIRM_PATH = "/api/v2/auth/password-reset/confirm";

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定），不依赖假计时器 */
function flush() {
  return act(async () => {});
}

/** entry 为含 query 的路径（/password-reset/confirm?token=） */
function renderConfirm(entry = "/password-reset/confirm?token=tok-1", { strict = false } = {}) {
  const tree = (
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/password-reset/confirm" element={<PasswordResetConfirmPage />} />
        <Route path="/password-reset" element={<div>重置请求页落点</div>} />
        <Route path="/login" element={<div>登录页落点</div>} />
      </Routes>
    </MemoryRouter>
  );
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

function fillForm({ password = "new-secret-123", confirm = password } = {}) {
  fireEvent.change(screen.getByLabelText("新密码"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("确认新密码"), { target: { value: confirm } });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "重置密码" }));
}

function confirmCalls() {
  return requestV2.mock.calls.filter(([path]) => path === CONFIRM_PATH);
}

beforeEach(() => {
  vi.resetAllMocks();
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("提交 → 200 成功页", () => {
  it("一致性通过 → confirm 收到 {reset_token, new_password} + 幂等键；成功页显「密码已重置，请重新登录」+ 前往登录", async () => {
    requestV2.mockResolvedValueOnce({ status: 200, data: { ok: true }, headers: new Headers() });
    renderConfirm();
    fillForm();
    submit();
    await flush();

    const [path, options] = confirmCalls()[0];
    expect(path).toBe(CONFIRM_PATH);
    expect(options.method).toBe("POST");
    expect(options.body).toEqual({ reset_token: "tok-1", new_password: "new-secret-123" });
    // 幂等义务端点（A5）：幂等键必带且 ≤100 字符
    expect(options.idempotencyKey).toBeTruthy();
    expect(options.idempotencyKey.length).toBeLessThanOrEqual(100);

    expect(screen.getByText("密码已重置，请重新登录")).toBeTruthy();
    expect(screen.queryByLabelText("新密码")).toBeNull();
    const loginLink = screen.getByRole("link", { name: "前往登录" });
    expect(loginLink.getAttribute("href")).toBe("/login?v2=1");
  });
});

describe("前端一致性校验", () => {
  it("必填门：空表单提交 → 两处字段级错误，不发请求", async () => {
    renderConfirm();
    submit();
    await flush();

    expect(screen.getAllByRole("alert")).toHaveLength(2);
    expect(requestV2).not.toHaveBeenCalled();
  });

  it("两次密码不一致 → 确认字段报错「两次输入的密码不一致」，不发请求", async () => {
    renderConfirm();
    fillForm({ confirm: "different-456" });
    submit();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("两次输入的密码不一致");
    expect(requestV2).not.toHaveBeenCalled();
  });
});

describe("失败分流", () => {
  it("400 EMAIL_NOT_VERIFIED → 失效页「链接无效或已过期」（防探测统一形态）", async () => {
    const error = new Error("链接无效或已过期");
    error.name = "V2ApiError";
    error.code = "EMAIL_NOT_VERIFIED";
    error.status = 400;
    requestV2.mockRejectedValueOnce(error);
    renderConfirm();
    fillForm();
    submit();
    await flush();

    expect(screen.getByText("链接无效或已过期")).toBeTruthy();
    expect(screen.queryByLabelText("新密码")).toBeNull();
  });

  it("query 无 token → 直接失效页，不发请求", () => {
    renderConfirm("/password-reset/confirm");
    expect(screen.getByText("链接无效或已过期")).toBeTruthy();
    expect(requestV2).not.toHaveBeenCalled();
  });

  it("其它失败（如 500）→ 内联 alert 留在表单；重试复用同一把幂等键", async () => {
    const error = new Error("请求失败（HTTP 500）");
    error.name = "V2ApiError";
    error.code = "HTTP_500";
    error.status = 500;
    requestV2.mockRejectedValueOnce(error);
    renderConfirm();
    fillForm();
    submit();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("请求失败（HTTP 500）");
    expect(screen.getByLabelText("新密码")).toBeTruthy();

    requestV2.mockResolvedValueOnce({ status: 200, data: { ok: true }, headers: new Headers() });
    submit();
    await flush();
    expect(screen.getByText("密码已重置，请重新登录")).toBeTruthy();

    const calls = confirmCalls();
    expect(calls).toHaveLength(2);
    // 同一逻辑操作（同一令牌的密码重置）：重试必须复用同一把 Idempotency-Key
    expect(calls[0][1].idempotencyKey).toBeTruthy();
    expect(calls[0][1].idempotencyKey).toBe(calls[1][1].idempotencyKey);
  });
});

describe("React.StrictMode 双执行陷阱", () => {
  it("双渲染下提交一次 confirm 仅发一次（useRef 初始化器幂等键 + 提交闸门）", async () => {
    requestV2.mockResolvedValueOnce({ status: 200, data: { ok: true }, headers: new Headers() });
    renderConfirm("/password-reset/confirm?token=tok-1", { strict: true });
    fillForm();
    submit();
    await flush();

    // StrictMode 双渲染不改写用户事件语义：confirm 恰好一次
    expect(confirmCalls()).toHaveLength(1);
    expect(confirmCalls()[0][1].idempotencyKey).toBeTruthy();
    expect(screen.getByText("密码已重置，请重新登录")).toBeTruthy();
  });
});
