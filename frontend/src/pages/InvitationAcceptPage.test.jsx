import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { requestV2 } from "../api/v2/client.js";
import InvitationAcceptPage from "./InvitationAcceptPage.jsx";

// 页面只消费 useAuth.acceptInvitation（accept+csrf+探测置位 v2User 在 AuthContext 层，
// 由 AuthContext.test.js 覆盖）；重发按钮走 useResendVerification → requestV2（在此打桩）
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

const acceptInvitationMock = vi.fn();

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

/** entry 为含 query 的路径（/invitations/accept?token=&email=） */
function renderAccept(entry) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/invitations/accept" element={<InvitationAcceptPage />} />
        <Route path="/login" element={<div>登录页落点</div>} />
      </Routes>
    </MemoryRouter>
  );
}

function fillForm({ email = "alice@example.com", password = "secret-123", confirm = password } = {}) {
  if (email !== null) {
    fireEvent.change(screen.getByLabelText("邮箱"), { target: { value: email } });
  }
  fireEvent.change(screen.getByLabelText("设置密码"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: confirm } });
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({ acceptInvitation: acceptInvitationMock });
  acceptInvitationMock.mockRejectedValue(new Error("acceptInvitation：本用例未显式编排"));
  requestV2.mockResolvedValue({ status: 200, data: { ok: true }, headers: new Headers() });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("表单渲染与 query 预填", () => {
  it("token 从 query 预填且只读；email 从 query 预填且可改", () => {
    renderAccept("/invitations/accept?token=tok-1&email=alice@example.com");

    const tokenInput = screen.getByLabelText("邀请令牌");
    expect(tokenInput.value).toBe("tok-1");
    expect(tokenInput.readOnly).toBe(true);

    const emailInput = screen.getByLabelText("邮箱");
    expect(emailInput.value).toBe("alice@example.com");
    expect(emailInput.readOnly).toBe(false);
    fireEvent.change(emailInput, { target: { value: "changed@example.com" } });
    expect(screen.getByLabelText("邮箱").value).toBe("changed@example.com");
  });

  it("query 无 email 参数 → 降级手输（outbox 白名单仅 {action_token, valid_hours}，前端不依赖 email 进 URL）", () => {
    renderAccept("/invitations/accept?token=tok-1");

    expect(screen.getByLabelText("邮箱").value).toBe("");
    fillForm();
    expect(screen.getByLabelText("邮箱").value).toBe("alice@example.com");
  });
});

describe("提交校验", () => {
  it("必填门：空表单提交 → 三处字段级错误，acceptInvitation 不被调用", async () => {
    renderAccept("/invitations/accept?token=tok-1");
    fireEvent.click(screen.getByRole("button", { name: "激活账户" }));
    await flush();

    expect(screen.getAllByRole("alert")).toHaveLength(3);
    expect(acceptInvitationMock).not.toHaveBeenCalled();
  });

  it("两次密码不一致 → 确认密码字段报错，acceptInvitation 不被调用", async () => {
    renderAccept("/invitations/accept?token=tok-1");
    fillForm({ confirm: "different-456" });
    fireEvent.click(screen.getByRole("button", { name: "激活账户" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("两次输入的密码不一致");
    expect(acceptInvitationMock).not.toHaveBeenCalled();
  });
});

describe("提交成功 → 引导页", () => {
  it("acceptInvitation 收到 trim 后的 email + password + token；探测确认 → 引导页含重发按钮", async () => {
    renderAccept("/invitations/accept?token=tok-1&email=alice@example.com");
    acceptInvitationMock.mockResolvedValueOnce({ sessionConfirmed: true });
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "激活账户" }));
    await flush();

    expect(acceptInvitationMock).toHaveBeenCalledWith("alice@example.com", "secret-123", "tok-1");
    expect(screen.getByText(/验证邮件已发送至 alice@example\.com/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "重发验证邮件" })).toBeTruthy();
    expect(screen.queryByLabelText("邮箱")).toBeNull();
  });

  it("重放态探测未确认（sessionConfirmed=false）→ 引导文案在、重发按钮与一切认证端点按钮消失、改显前往登录链接", async () => {
    renderAccept("/invitations/accept?token=tok-1&email=alice@example.com");
    acceptInvitationMock.mockResolvedValueOnce({ sessionConfirmed: false });
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "激活账户" }));
    await flush();

    expect(screen.getByText(/验证邮件已发送至 alice@example\.com/)).toBeTruthy();
    expect(screen.getByText("请登录后在顶部横幅中重发验证邮件")).toBeTruthy();
    const loginLink = screen.getByRole("link", { name: "前往登录" });
    expect(loginLink.getAttribute("href")).toBe("/login?v2=1");
    // 钉死：该分支不得渲染任何调用认证端点的按钮（resend 为认证端点，无会话必 401）
    expect(screen.queryAllByRole("button")).toEqual([]);
  });

  it("引导页重发按钮 → POST email-verification/resend（认证端点自动 CSRF）并进入冷却", async () => {
    renderAccept("/invitations/accept?token=tok-1&email=alice@example.com");
    acceptInvitationMock.mockResolvedValueOnce({ sessionConfirmed: true });
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "激活账户" }));
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "重发验证邮件" }));
    await flush();

    expect(requestV2).toHaveBeenCalledWith("/api/v2/auth/email-verification/resend", {
      method: "POST",
    });
    expect(screen.getByRole("button", { name: /重新发送（60s）/ }).disabled).toBe(true);
  });
});

describe("错误分流", () => {
  it("409 INVITATION_INVALID → 失效态页（无入口按钮）", async () => {
    renderAccept("/invitations/accept?token=tok-1&email=alice@example.com");
    acceptInvitationMock.mockRejectedValueOnce(
      v2ApiError("INVITATION_INVALID", "邀请链接无效或已失效", 409)
    );
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "激活账户" }));
    await flush();

    expect(screen.getByText("邀请链接无效或已失效")).toBeTruthy();
    // 钉死：失效态页无入口按钮
    expect(screen.queryAllByRole("button")).toEqual([]);
  });

  it("429 → Retry-After 秒倒计时禁用提交按钮，归零自动解除", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    renderAccept("/invitations/accept?token=tok-1&email=alice@example.com");
    acceptInvitationMock.mockRejectedValueOnce(
      v2ApiError("RATE_LIMITED", "请求过于频繁，请稍后再试", 429, 30)
    );
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "激活账户" }));
    await flush();

    expect(screen.getByRole("button", { name: "激活账户" }).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("30");

    act(() => {
      vi.advanceTimersByTime(30000);
    });
    expect(screen.getByRole("button", { name: "激活账户" }).disabled).toBe(false);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("其它错误（如 500）→ 表单内联 alert，停留表单", async () => {
    renderAccept("/invitations/accept?token=tok-1&email=alice@example.com");
    acceptInvitationMock.mockRejectedValueOnce(v2ApiError("HTTP_500", "请求失败（HTTP 500）", 500));
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: "激活账户" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("请求失败（HTTP 500）");
    expect(screen.getByLabelText("邮箱")).toBeTruthy();
  });
});
