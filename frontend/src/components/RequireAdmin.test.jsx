import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import RequireAdmin, { useAdminGate } from "./RequireAdmin.jsx";

// RequireAdmin 三分流 + 403 数据面分流（Phase 8 T12a，测试清单①②）：
// 软门 role 判定 / authReady null / ADMIN_MFA_REQUIRED 两分支（未配置引导注册、
// 已配置 TOTP verify）/ FORBIDDEN 重探测踢回 / verify 成功后原请求重放。
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

/** 捕获 gate API 的探针子组件（领地内页面同款消费方式） */
let gateRef = null;
function GateProbe() {
  gateRef = useAdminGate();
  return <div>管理台内容标记</div>;
}

const ADMIN_USER = {
  id: "u-admin",
  email: "admin@example.com",
  role: "admin",
  status: "active",
  mfaEnabled: true,
};

/** 路由树单例（rerender 复用同一树——门状态跨 rerender 保留） */
function guardTree() {
  return (
    <MemoryRouter initialEntries={["/admin/users"]}>
      <Routes>
        <Route
          path="/admin/users"
          element={
            <RequireAdmin>
              <GateProbe />
            </RequireAdmin>
          }
        />
        <Route path="/" element={<div>首页落点</div>} />
      </Routes>
    </MemoryRouter>
  );
}

function renderGuard(authOverrides = {}) {
  useAuth.mockReturnValue({
    authReady: true,
    v2User: ADMIN_USER,
    refreshV2User: vi.fn().mockResolvedValue(ADMIN_USER),
    ...authOverrides,
  });
  return render(guardTree());
}

/** 在领地内触发一次 403 上报，返回 reportAdminError 的布尔结果 */
async function reportGateError(error, replay) {
  let handled;
  await act(async () => {
    handled = gateRef.reportAdminError(error, replay);
  });
  return handled;
}

function mfaError() {
  return new V2ApiError("ADMIN_MFA_REQUIRED", "需要管理员两步验证", 403);
}

beforeEach(() => {
  vi.resetAllMocks();
  gateRef = null;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("软门三分流（①）", () => {
  it("非 admin（v2User 无 role）→ Navigate /，children 不渲染", () => {
    renderGuard({ v2User: { ...ADMIN_USER, role: "user" } });
    expect(screen.queryByText("管理台内容标记")).toBeNull();
    expect(screen.getByText("首页落点")).toBeTruthy();
  });

  it("v2User 为 null（会话探测降级）→ Navigate /", () => {
    renderGuard({ v2User: null });
    expect(screen.getByText("首页落点")).toBeTruthy();
  });

  it("authReady 未落定 → 渲染 null（探测窗口不闪烁）", () => {
    renderGuard({ authReady: false });
    expect(screen.queryByText("管理台内容标记")).toBeNull();
    expect(screen.queryByText("首页落点")).toBeNull();
  });

  it("role=admin → children 放行，gate API 可用", () => {
    renderGuard();
    expect(screen.getByText("管理台内容标记")).toBeTruthy();
    expect(typeof gateRef.reportAdminError).toBe("function");
  });
});

describe("403 ADMIN_MFA_REQUIRED 分流（①）", () => {
  it("mfaEnabled=false → 渲染 TOTP 注册引导（MfaCard 内嵌），不重放", async () => {
    const replay = vi.fn();
    renderGuard({ v2User: { ...ADMIN_USER, mfaEnabled: false } });
    const handled = await reportGateError(mfaError(), replay);

    expect(handled).toBe(true);
    // MfaCard 未启用态的入口按钮
    expect(screen.getByRole("button", { name: "开始设置" })).toBeTruthy();
    expect(replay).not.toHaveBeenCalled();
  });

  it("mfaEnabled=true → 渲染 TOTP verify 卡（12h 续期），不立即重放", async () => {
    const replay = vi.fn();
    renderGuard();
    const handled = await reportGateError(mfaError(), replay);

    expect(handled).toBe(true);
    expect(screen.getByLabelText("两步验证码")).toBeTruthy();
    expect(screen.getByRole("button", { name: "验证并继续" })).toBeTruthy();
    expect(replay).not.toHaveBeenCalled();
  });

  it("非 V2ApiError / 非 403 / 其它 403 code → reportAdminError 返回 false 不接管", async () => {
    renderGuard();
    expect(await reportGateError(new Error("网络错误"), vi.fn())).toBe(false);
    expect(
      await reportGateError(new V2ApiError("VALIDATION_ERROR", "x", 400), vi.fn())
    ).toBe(false);
    expect(
      await reportGateError(new V2ApiError("CSRF_INVALID", "x", 403), vi.fn())
    ).toBe(false);
    expect(screen.getByText("管理台内容标记")).toBeTruthy();
  });
});

describe("verify 成功后原请求重放（②）", () => {
  it("POST /api/auth/mfa/verify {totp_code} 成功 → 清门 + 重放一次", async () => {
    requestV2.mockResolvedValue({ status: 200, data: { mfa_verified: true } });
    const replay = vi.fn();
    renderGuard();
    await reportGateError(mfaError(), replay);

    fireEvent.change(screen.getByLabelText("两步验证码"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并继续" }));
    await waitFor(() => expect(replay).toHaveBeenCalledTimes(1));

    expect(requestV2).toHaveBeenCalledWith("/api/auth/mfa/verify", {
      method: "POST",
      body: { totp_code: "123456" },
    });
    // 门已清：children 回归
    expect(screen.getByText("管理台内容标记")).toBeTruthy();
    expect(screen.queryByLabelText("两步验证码")).toBeNull();
  });

  it("401 MFA_INVALID → 内联报错，不重放", async () => {
    requestV2.mockRejectedValue(new V2ApiError("MFA_INVALID", "动态码不正确", 401));
    const replay = vi.fn();
    renderGuard();
    await reportGateError(mfaError(), replay);

    fireEvent.change(screen.getByLabelText("两步验证码"), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并继续" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());

    expect(screen.getByRole("alert").textContent).toContain("动态码不正确");
    expect(replay).not.toHaveBeenCalled();
  });

  it("400 MFA_NOT_CONFIGURED（本地 mfaEnabled 陈旧）→ 重探测收敛后降级注册引导，不重放", async () => {
    requestV2.mockRejectedValue(new V2ApiError("MFA_NOT_CONFIGURED", "未配置", 400));
    const replay = vi.fn();
    const view = renderGuard();
    await reportGateError(mfaError(), replay);

    fireEvent.change(screen.getByLabelText("两步验证码"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并继续" }));
    await waitFor(() =>
      expect(requestV2).toHaveBeenCalledWith(
        "/api/auth/mfa/verify",
        expect.objectContaining({ method: "POST" })
      )
    );

    // NOT_CONFIGURED 分支内部 refreshV2User 收敛上下文（真实 AuthContext 更新
    // v2User.mfaEnabled=false；此处模拟收敛后的 rerender）
    useAuth.mockReturnValue({
      authReady: true,
      v2User: { ...ADMIN_USER, mfaEnabled: false },
      refreshV2User: vi.fn(),
    });
    await act(async () => {
      view.rerender(guardTree());
    });

    expect(screen.getByRole("button", { name: "开始设置" })).toBeTruthy();
    expect(replay).not.toHaveBeenCalled();
  });

  it("注册完成（activate 后 mfaEnabled 翻转）→ 自动重放原请求", async () => {
    const replay = vi.fn();
    const refreshV2User = vi.fn().mockResolvedValue({ ...ADMIN_USER, mfaEnabled: true });
    const view = renderGuard({ v2User: { ...ADMIN_USER, mfaEnabled: false }, refreshV2User });
    await reportGateError(mfaError(), replay);
    expect(replay).not.toHaveBeenCalled();

    // MfaCard activate 成功内部 refreshV2User → 上下文翻转 mfaEnabled=true（此处
    // 模拟翻转后的 rerender），门监听翻转自动续跑原请求（会话已被 activate 盖戳）
    useAuth.mockReturnValue({
      authReady: true,
      v2User: { ...ADMIN_USER, mfaEnabled: true },
      refreshV2User,
    });
    await act(async () => {
      view.rerender(guardTree());
    });
    await waitFor(() => expect(replay).toHaveBeenCalledTimes(1));
    expect(screen.getByText("管理台内容标记")).toBeTruthy();
  });
});

describe("403 FORBIDDEN 处理（R9）", () => {
  it("重探测 refreshV2User + 踢回首页，children 不再渲染", async () => {
    const refreshV2User = vi.fn().mockResolvedValue(null);
    renderGuard({ refreshV2User });
    const replay = vi.fn();
    const handled = await reportGateError(
      new V2ApiError("FORBIDDEN", "需要管理员权限", 403),
      replay
    );

    expect(handled).toBe(true);
    await waitFor(() => expect(refreshV2User).toHaveBeenCalledTimes(1));
    expect(screen.getByText("首页落点")).toBeTruthy();
    expect(screen.queryByText("管理台内容标记")).toBeNull();
    expect(replay).not.toHaveBeenCalled();
  });
});
