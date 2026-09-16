import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import RequireAuth from "./RequireAuth.jsx";

// 路由守卫（Phase 8 T11 ④/T14 收敛）：requireExpert 消费上下文单判据 isExpert——
// V2 expert_author（entitlements 含 expert_author）可入专家页；无 V2 会话弹 /login。
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));

const V2_USER = { id: "u-2", email: "v2@example.com", role: "user", status: "active" };

function renderGuard(authOverrides = {}) {
  useAuth.mockReturnValue({
    authReady: true,
    v2User: null,
    isExpert: false,
    ...authOverrides,
  });
  return render(
    <MemoryRouter initialEntries={["/my-experts"]}>
      <Routes>
        <Route
          path="/my-experts"
          element={
            <RequireAuth requireExpert>
              <div>专家页落点</div>
            </RequireAuth>
          }
        />
        <Route path="/" element={<div>首页落点</div>} />
        <Route path="/login" element={<div>登录页落点</div>} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("requireExpert 判据 = V2 entitlements 含 expert_author（T11 ④/T14 单判据）", () => {
  it("V2 expert_author：放行专家页", () => {
    renderGuard({
      v2User: { ...V2_USER, entitlements: ["expert_author"] },
      isExpert: true,
    });
    expect(screen.getByText("专家页落点")).toBeTruthy();
    expect(screen.queryByText("首页落点")).toBeNull();
  });

  it("V2 无 entitlement：弹回首页（专家页不可入）", () => {
    renderGuard({ v2User: V2_USER, isExpert: false });
    expect(screen.queryByText("专家页落点")).toBeNull();
    expect(screen.getByText("首页落点")).toBeTruthy();
  });

  it("无 V2 会话（匿名）：弹回 /login（V1 会话域已随 cutover 删除）", () => {
    renderGuard();
    expect(screen.queryByText("专家页落点")).toBeNull();
    expect(screen.getByText("登录页落点")).toBeTruthy();
  });

  it("authReady 未落定：渲染 null（探测窗口不闪烁）", () => {
    renderGuard({ authReady: false });
    expect(screen.queryByText("专家页落点")).toBeNull();
    expect(screen.queryByText("登录页落点")).toBeNull();
  });
});
