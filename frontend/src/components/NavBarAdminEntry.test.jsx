import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import NavBar from "./NavBar.jsx";

// NavBar admin 条件入口（Phase 8 T12a）：仅 v2User.role==='admin' 渲染「管理」
// 导航链接；非 admin / 匿名 / V1-only 会话一律不渲染（T11 既有行为不受影响）。
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));

function renderNav(authOverrides = {}) {
  useAuth.mockReturnValue({
    user: null,
    v2User: null,
    isAuthenticated: false,
    isExpert: false,
    logout: vi.fn(),
    logoutV2: vi.fn(),
    ...authOverrides,
  });
  return render(
    <MemoryRouter>
      <NavBar />
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("管理入口条件渲染（T12a）", () => {
  it("v2User.role==='admin' → 渲染「管理」链接指向 /admin", () => {
    renderNav({ v2User: { email: "a@x.com", role: "admin", status: "active" } });
    const link = screen.getByRole("link", { name: "管理" });
    expect(link.getAttribute("href")).toBe("/admin");
  });

  it("V2 普通用户不渲染管理入口", () => {
    renderNav({ v2User: { email: "a@x.com", role: "user", status: "active" } });
    expect(screen.queryByRole("link", { name: "管理" })).toBeNull();
  });

  it("匿名与 V1-only 会话不渲染管理入口（V1 role 与 admin 面无关）", () => {
    renderNav();
    expect(screen.queryByRole("link", { name: "管理" })).toBeNull();

    renderNav({ user: { id: 1, username: "alice", email: "a@x.com", role: "expert" } });
    expect(screen.queryByRole("link", { name: "管理" })).toBeNull();
  });
});
