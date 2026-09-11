import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "./api/client.js";
import { requestV2 } from "./api/v2/client.js";
import { AuthProvider } from "./auth/AuthContext.jsx";
import AppRoutes from "./router.jsx";

// 路由接线测试：真实 AuthProvider + 真实 AppRoutes，网络入口整体打桩。
// V1 request 供 HomePage 等壳内页面挂载加载（空列表即可）；V2 requestV2 编排探测流。
vi.mock("./api/client.js", () => ({
  getToken: vi.fn(() => null),
  setToken: vi.fn(),
  request: vi.fn(),
}));

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

beforeEach(() => {
  vi.resetAllMocks();
  // 默认匿名（V2 启动探测 401 silent）+ V1 壳内加载空列表
  requestV2.mockRejectedValue(V2_SESSION_EXPIRED());
  request.mockResolvedValue({ data: [], total: 0 });
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
