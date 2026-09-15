import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { request } from "./api/client.js";
import { requestV2 } from "./api/v2/client.js";
import { AuthProvider } from "./auth/AuthContext.jsx";
import AppRoutes from "./router.jsx";

// /admin 路由树接线测试（Phase 8 T12a）：真实 AuthProvider + 真实 AppRoutes、
// 网络入口整体打桩——/admin 索引重定向到 /admin/invitations；admin 会话可达
// 邀请页并触发列表装载；非 admin/匿名经 RequireAdmin 弹回首页。
vi.mock("./api/client.js", () => ({
  getToken: vi.fn(() => null),
  setToken: vi.fn(),
  request: vi.fn(),
}));

vi.mock("./api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn(), setCsrfToken: vi.fn() };
});

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location-path">{location.pathname}</span>;
}

function renderAt(entry) {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={[entry]}>
        <LocationProbe />
        <AppRoutes />
      </MemoryRouter>
    </AuthProvider>
  );
}

const ADMIN_RAW = {
  id: "u-admin",
  email: "admin@example.com",
  role: "admin",
  status: "active",
  mfa_enabled: true,
};
const EMPTY_INVITATIONS = { items: [], total: 0, page: 1, size: 20 };

beforeEach(() => {
  vi.resetAllMocks();
  request.mockResolvedValue({ data: [], total: 0 });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("/admin 路由树（T12a）", () => {
  it("admin 会话：/admin 索引重定向 /admin/invitations 并装载邀请列表", async () => {
    requestV2.mockImplementation((path) => {
      if (path === "/api/v2/users/me") {
        return Promise.resolve(ok(ADMIN_RAW));
      }
      if (path === "/api/admin/invitations") {
        return Promise.resolve(ok(EMPTY_INVITATIONS));
      }
      return Promise.reject(new Error(`未编排的请求：${path}`));
    });

    renderAt("/admin");

    await waitFor(() =>
      expect(requestV2).toHaveBeenCalledWith("/api/admin/invitations", expect.anything())
    );
    expect(screen.getByTestId("location-path").textContent).toBe("/admin/invitations");
    expect(screen.getByRole("link", { name: "邀请管理" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "用户管理" })).toBeTruthy();
  });

  it("V2 普通用户访问 /admin → RequireAdmin 弹回首页", async () => {
    requestV2.mockImplementation((path) => {
      if (path === "/api/v2/users/me") {
        return Promise.resolve(ok({ ...ADMIN_RAW, role: "user" }));
      }
      return Promise.reject(new Error(`未编排的请求：${path}`));
    });

    renderAt("/admin/invitations");

    await waitFor(() =>
      expect(screen.getByTestId("location-path").textContent).toBe("/")
    );
    expect(requestV2).not.toHaveBeenCalledWith(
      "/api/admin/invitations",
      expect.anything()
    );
  });

  it("匿名（探测 401）访问 /admin → 不可达（链式收敛至登录页）", async () => {
    const expired = new Error("登录状态已失效，请重新登录");
    expired.name = "V2ApiError";
    expired.code = "SESSION_EXPIRED";
    expired.status = 401;
    requestV2.mockRejectedValue(expired);

    renderAt("/admin");

    // RequireAdmin（v2User=null）弹回 / 后，首页守卫 RequireAuth 继续收敛至 /login
    await waitFor(() =>
      expect(screen.getByTestId("location-path").textContent).toBe("/login")
    );
  });
});
