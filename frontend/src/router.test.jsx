import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { requestV2 } from "./api/v2/client.js";
import { AuthProvider } from "./auth/AuthContext.jsx";
import AppRoutes from "./router.jsx";

// 路由接线测试：真实 AuthProvider + 真实 AppRoutes，网络入口整体打桩。
// V2 requestV2 编排探测流（T14 会话归一：唯一网络入口）。
vi.mock("./api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn(), setCsrfToken: vi.fn() };
});

const V2_SESSION_EXPIRED = () => {
  const error = new Error("登录状态已失效，请重新登录");
  error.name = "V2ApiError";
  error.code = "SESSION_EXPIRED";
  error.status = 401;
  return error;
};

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

function renderAt(entry) {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={[entry]}>
        <AppRoutes />
      </MemoryRouter>
    </AuthProvider>
  );
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location-path">{location.pathname}</span>;
}

beforeEach(() => {
  vi.resetAllMocks();
  // 默认匿名（V2 启动探测 401 silent）
  requestV2.mockRejectedValue(V2_SESSION_EXPIRED());
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("令牌驱动公开流不挂守卫（E11）", () => {
  it("匿名访问 /invitations/accept?token=&email= → 接受邀请表单渲染，不弹回", async () => {
    renderAt("/invitations/accept?token=tok-1&email=alice@example.com");

    await waitFor(() => expect(screen.getByLabelText("邀请令牌")).toBeTruthy());
    expect(screen.getByLabelText("邀请令牌").value).toBe("tok-1");
    expect(screen.queryByLabelText("用户名或邮箱")).toBeNull();
  });

  it("匿名访问 /email-verification?token= → confirm 200 → 成功页渲染，不弹回 /login", async () => {
    requestV2.mockResolvedValueOnce(ok({ ok: true })); // confirm
    renderAt("/email-verification?token=tok-1");

    await waitFor(() => expect(screen.getByText("邮箱验证成功")).toBeTruthy());
    expect(screen.queryByLabelText("邀请令牌")).toBeNull();
  });

  it("匿名访问 /password-reset → 重置请求表单渲染，不弹回", () => {
    renderAt("/password-reset");
    expect(screen.getByLabelText("邮箱")).toBeTruthy();
    expect(screen.queryByLabelText("用户名或邮箱")).toBeNull();
  });

  it("匿名访问 /password-reset/confirm?token= → 重置表单渲染，不弹回", () => {
    renderAt("/password-reset/confirm?token=tok-1");
    expect(screen.getByLabelText("新密码")).toBeTruthy();
  });

  it("匿名访问 /account/deletion/cancel?token= → 撤销注销表单渲染，不弹回", () => {
    renderAt("/account/deletion/cancel?token=tok-1");
    expect(screen.getByLabelText("登录密码")).toBeTruthy();
  });
});

describe("pending 横幅挂载于应用壳（NavBar 之下）", () => {
  it("pending 会话访问 / → NavBar 与「邮箱尚未验证」横幅同时渲染", async () => {
    requestV2.mockResolvedValueOnce(
      ok({ id: "u-9", email: "pending@example.com", role: "user", status: "pending" })
    );
    renderAt("/");

    await waitFor(() => expect(screen.getByText("邮箱尚未验证")).toBeTruthy());
    expect(screen.getByRole("button", { name: "重发验证邮件" })).toBeTruthy();
    // NavBar 接线仍在（横幅在其下方）
    expect(screen.getByLabelText("AgentCraft 首页")).toBeTruthy();
  });
});

describe("注销受理冻结页路由（终审修复：T2×T7 接缝）", () => {
  /** V2-only 会话全链路编排：启动探测 users/me + 会话列表 + 注销受理 */
  function mockV2SessionFlow() {
    requestV2.mockImplementation((url) => {
      if (url.endsWith("/users/me")) {
        return Promise.resolve(
          ok({ id: "u-2", email: "v2@example.com", role: "user", mfa_enabled: false })
        );
      }
      if (url.includes("/auth/sessions")) {
        return Promise.resolve(ok([]));
      }
      if (url.includes("/deletion/request")) {
        return Promise.resolve(ok({ status: "deleting", days_remaining: 14 }));
      }
      return Promise.reject(new Error(`requestV2：未编排的端点 ${url}`));
    });
  }

  it("V2-only 用户 /profile 注销成功 → 落在 /account/deleting，不被 RequireAuth 弹回 /login", async () => {
    mockV2SessionFlow();
    render(
      <AuthProvider>
        <MemoryRouter initialEntries={["/profile"]}>
          <AppRoutes />
          <LocationProbe />
        </MemoryRouter>
      </AuthProvider>
    );

    // 真实 AuthProvider 探测落定 → ProfilePage（含危险区）渲染
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "申请注销账户" })).toBeTruthy()
    );
    fireEvent.click(screen.getByRole("button", { name: "申请注销账户" }));
    await waitFor(() => expect(screen.getByLabelText("登录密码")).toBeTruthy());
    fireEvent.change(screen.getByLabelText("登录密码"), {
      target: { value: "pw-123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));

    // 受理成功 clearV2Session（v2User→null）与导航同批提交：冻结页不挂守卫，
    // 匿名到达不被弹回（location 终态钉死在 /account/deleting）
    await waitFor(() => expect(screen.getByText("账户注销中")).toBeTruthy());
    expect(screen.getByText(/14 天后生效/)).toBeTruthy();
    expect(screen.getByTestId("location-path").textContent).toBe("/account/deleting");
  });

  it("匿名直达 /account/deleting → 冻结页渲染，不弹回 /login（缺省 14 天）", () => {
    renderAt("/account/deleting");

    expect(screen.getByText("账户注销中")).toBeTruthy();
    expect(screen.getByText(/14 天后生效/)).toBeTruthy();
    expect(screen.queryByLabelText("登录密码")).toBeNull();
  });
});
