import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import NavBar from "./NavBar.jsx";

// NavBar admin 条件入口（Phase 8 T12a）：仅 v2User.role==='admin' 渲染「管理」
// 导航链接；非 admin / 匿名一律不渲染。
// mock 形状对齐 V2-only 会话域（cutover 审查 M2 清理）：T14 会话归一后组件仅
// 消费 v2User/isExpert/logoutV2，V1 会话键（user/isAuthenticated/logout）已消亡。
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));

function renderNav(authOverrides = {}) {
  useAuth.mockReturnValue({
    v2User: null,
    isExpert: false,
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

  it("匿名会话不渲染管理入口", () => {
    renderNav();
    expect(screen.queryByRole("link", { name: "管理" })).toBeNull();
  });
});
