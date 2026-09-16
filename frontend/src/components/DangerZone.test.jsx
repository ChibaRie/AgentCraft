import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import AccountDeletingPage from "../pages/AccountDeletingPage.jsx";
import DangerZone from "./DangerZone.jsx";

// 卡片级行为测试：mock useAuth（clearV2Session 观察点）+ requestV2 网络入口
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

/** 真实 V2ApiError 实例（组件以 instanceof 区分网络层裸错误的兜底路径） */
function v2Error(code, message, status, retryAfter) {
  return new V2ApiError(code, message, status, retryAfter);
}

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定） */
function flush() {
  return act(async () => {});
}

const V2_USER_OFF = {
  id: "u-2",
  email: "v2@example.com",
  role: "user",
  status: "active",
  mfaEnabled: false,
};
const V2_USER_ON = { ...V2_USER_OFF, mfaEnabled: true };

/** 挂在 /profile 路由下渲染（useNavigate 消费真实 Router；/account/deleting
 *  接真实冻结页——注销成功后可断言 location 终态）。 */
function renderZone() {
  return render(
    <MemoryRouter initialEntries={["/profile"]}>
      <Routes>
        <Route path="/profile" element={<DangerZone />} />
        <Route path="/account/deleting" element={<AccountDeletingPage />} />
      </Routes>
    </MemoryRouter>
  );
}

/** 打开二次确认弹层 */
async function openConfirm() {
  renderZone();
  fireEvent.click(screen.getByRole("button", { name: "申请注销账户" }));
  await flush();
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({ v2User: V2_USER_OFF, clearV2Session: vi.fn() });
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("危险面板与二次确认弹层", () => {
  it("渲染 14 天宽限期说明 + 申请入口；弹层默认关闭", () => {
    renderZone(vi.fn());

    expect(screen.getByText(/14 天宽限期/)).toBeTruthy();
    expect(screen.getByText(/邮件中的恢复链接|恢复链接/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "申请注销账户" })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("点击申请入口 → 二次确认弹层（role=dialog）出现，含密码再认证表单", async () => {
    await openConfirm();

    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByLabelText("登录密码")).toBeTruthy();
    expect(screen.getByRole("button", { name: "确认注销" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "取消" })).toBeTruthy();
  });

  it("取消 → 弹层关闭，可重新打开", async () => {
    await openConfirm();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await flush();
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "申请注销账户" }));
    await flush();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("mfaEnabled 用户弹层内出现两步验证码输入；未启用用户无", async () => {
    const off = renderZone();
    fireEvent.click(screen.getByRole("button", { name: "申请注销账户" }));
    await flush();
    expect(screen.queryByLabelText("两步验证码")).toBeNull();
    off.unmount();

    useAuth.mockReturnValue({ v2User: V2_USER_ON, clearV2Session: vi.fn() });
    renderZone();
    fireEvent.click(screen.getByRole("button", { name: "申请注销账户" }));
    await flush();
    expect(screen.getByLabelText("两步验证码")).toBeTruthy();
  });
});

describe("提交门与请求形状", () => {
  it("空密码提交 → 客户端拦截（错误文案），不发请求", async () => {
    await openConfirm();

    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    await flush();

    expect(screen.getByText("请输入登录密码")).toBeTruthy();
    expect(requestV2).not.toHaveBeenCalled();
  });

  it("mfaEnabled 且验证码不足 → 拦截；补齐后 POST 携带 totp_code + 幂等键", async () => {
    useAuth.mockReturnValue({ v2User: V2_USER_ON, clearV2Session: vi.fn() });
    renderZone(vi.fn());
    fireEvent.click(screen.getByRole("button", { name: "申请注销账户" }));
    await flush();

    fireEvent.change(screen.getByLabelText("登录密码"), {
      target: { value: "pw-123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    await flush();
    expect(screen.getByText("请输入 6-8 位两步验证码")).toBeTruthy();
    expect(requestV2).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("两步验证码"), {
      target: { value: "123456" },
    });
    requestV2.mockResolvedValueOnce(ok({ status: "deleting", days_remaining: 14 }));
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    await flush();

    expect(requestV2).toHaveBeenCalledTimes(1);
    const [path, options] = requestV2.mock.calls[0];
    expect(path).toBe("/api/account/deletion/request");
    expect(options.method).toBe("POST");
    expect(options.body).toEqual({ password: "pw-123456", totp_code: "123456" });
    expect(typeof options.idempotencyKey).toBe("string");
  });

  it("未启用 MFA：body 不携带 totp_code 键（非 undefined 值）", async () => {
    await openConfirm();
    fireEvent.change(screen.getByLabelText("登录密码"), {
      target: { value: "pw-123456" },
    });
    requestV2.mockResolvedValueOnce(ok({ status: "deleting", days_remaining: 14 }));
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    await flush();

    const [, options] = requestV2.mock.calls[0];
    expect(options.body).toEqual({ password: "pw-123456" });
    expect("totp_code" in options.body).toBe(false);
  });
});

describe("注销受理流（200 → 本地 V2 态清理 + 导航冻结页路由）", () => {
  it("200 → clearV2Session + 落在 /account/deleting（days 透传冻结页）", async () => {
    renderZone();
    fireEvent.click(screen.getByRole("button", { name: "申请注销账户" }));
    await flush();
    fireEvent.change(screen.getByLabelText("登录密码"), {
      target: { value: "pw-123456" },
    });
    requestV2.mockResolvedValueOnce(ok({ status: "deleting", days_remaining: 14 }));
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    await flush();

    expect(useAuth().clearV2Session).toHaveBeenCalledTimes(1);
    // location 终态 = 独立冻结页路由（不再经页面回调渲染）
    expect(screen.getByText("账户注销中")).toBeTruthy();
    expect(screen.getByText(/14 天后生效/)).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("409 ACCOUNT_DELETING（宽限期内重复申请）→ 后端文案内联，不清理不导航", async () => {
    renderZone();
    fireEvent.click(screen.getByRole("button", { name: "申请注销账户" }));
    await flush();
    fireEvent.change(screen.getByLabelText("登录密码"), {
      target: { value: "pw-123456" },
    });
    requestV2.mockRejectedValueOnce(
      v2Error("ACCOUNT_DELETING", "注销处理中", 409)
    );
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    await flush();

    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toBe("注销处理中");
    expect(useAuth().clearV2Session).not.toHaveBeenCalled();
    // 未导航：仍在 /profile（危险卡片入口在、冻结页不在）
    expect(screen.getByRole("button", { name: "申请注销账户" })).toBeTruthy();
    expect(screen.queryByText("账户注销中")).toBeNull();
  });

  it("401 INVALID_CREDENTIALS（密码错误）→ 内联后端文案，留在表单", async () => {
    await openConfirm();
    fireEvent.change(screen.getByLabelText("登录密码"), {
      target: { value: "wrong-pw" },
    });
    requestV2.mockRejectedValueOnce(
      v2Error("INVALID_CREDENTIALS", "当前密码不正确", 401)
    );
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("当前密码不正确");
    expect(screen.getByLabelText("登录密码").value).toBe("wrong-pw");
  });

  it("网络层裸错误 → 兜底文案", async () => {
    await openConfirm();
    fireEvent.change(screen.getByLabelText("登录密码"), {
      target: { value: "pw-123456" },
    });
    requestV2.mockRejectedValueOnce(new Error("network down"));
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("操作失败，请稍后重试");
  });

  it("429 → Retry-After 倒计时禁用提交，归零自动解除", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    await openConfirm();
    fireEvent.change(screen.getByLabelText("登录密码"), {
      target: { value: "pw-123456" },
    });
    requestV2.mockRejectedValueOnce(
      v2Error("RATE_LIMITED", "操作过于频繁，请稍后再试", 429, 30)
    );
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    // 429 分支后 useRetryAfter 启动 interval：假计时器下排空微任务
    await act(async () => {});

    expect(screen.getByRole("button", { name: "确认注销" }).disabled).toBe(true);
    expect(screen.getByRole("status").textContent).toContain("30");

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole("status").textContent).toContain("28");

    act(() => {
      vi.advanceTimersByTime(28000);
    });
    expect(screen.getByRole("button", { name: "确认注销" }).disabled).toBe(false);
  });
});
