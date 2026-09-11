import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { request } from "../api/client.js";
import { requestV2 } from "../api/v2/client.js";
import AccountDeletingPage from "./AccountDeletingPage.jsx";
import ProfilePage from "./ProfilePage.jsx";

// 页面装配冒烟：身份/角色渲染源、安全区块 gating 与注销受理导航终态
// （卡片行为在各自组件测试覆盖；冻结页本体在 AccountDeletingPage.test）
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/client.js", () => ({ request: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

/** render + 排空微任务（任务列表/会话列表 effect 异步落定，避免 act 告警） */
async function renderProfile() {
  const view = render(
    <MemoryRouter initialEntries={["/profile"]}>
      <Routes>
        <Route path="/profile" element={<ProfilePage />} />
      </Routes>
    </MemoryRouter>
  );
  await act(async () => {});
  return view;
}

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

const BASE_AUTH = {
  user: null,
  v2User: null,
  isExpert: false,
  applyExpert: vi.fn(),
  refreshV2User: vi.fn(),
  clearV2Session: vi.fn(),
};

beforeEach(() => {
  vi.resetAllMocks();
  request.mockResolvedValue({ data: [] });
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("角色渲染源（T2 账本 minor：V2 admin 曾显示 user）", () => {
  it("仅 V2 会话 admin：角色行显示 admin + 管理员徽标", async () => {
    useAuth.mockReturnValue({
      ...BASE_AUTH,
      v2User: { id: "u-9", email: "root@example.com", role: "admin", status: "active" },
    });
    await renderProfile();

    expect(screen.getByText("admin")).toBeTruthy();
    expect(screen.getByText("管理员")).toBeTruthy();
    expect(screen.queryByText("普通用户")).toBeNull();
  });

  it("V1 expert：专家徽标 + expert 角色行（V1 域行为回归钉死）", async () => {
    useAuth.mockReturnValue({
      ...BASE_AUTH,
      user: { id: 1, username: "alice", email: "a@example.com", role: "expert" },
      isExpert: true,
    });
    await renderProfile();

    expect(screen.getByText("专家")).toBeTruthy();
    expect(screen.getByText("expert")).toBeTruthy();
  });
});

describe("安全区块 gating（端点均为 V2 会话语义）", () => {
  it("有 V2 会话（含双轨）：渲染设备/密码/两步验证/注销四卡片", async () => {
    useAuth.mockReturnValue({
      ...BASE_AUTH,
      user: { id: 1, username: "alice", email: "a@example.com", role: "user" },
      v2User: { id: "u-2", email: "v2@example.com", role: "user", mfaEnabled: false },
    });
    await renderProfile();

    expect(screen.getByText("设备与登录")).toBeTruthy();
    expect(screen.getByText("密码修改")).toBeTruthy();
    expect(screen.getByText("两步验证")).toBeTruthy();
    expect(screen.getByText("注销账户")).toBeTruthy();
  });

  it("仅 V1 会话：不渲染安全区块（避免对无 V2 会话者暴露必 401 的操作面）", async () => {
    useAuth.mockReturnValue({
      ...BASE_AUTH,
      user: { id: 1, username: "alice", email: "a@example.com", role: "user" },
    });
    await renderProfile();

    expect(screen.queryByText("设备与登录")).toBeNull();
    expect(screen.queryByText("密码修改")).toBeNull();
    expect(screen.queryByText("两步验证")).toBeNull();
    expect(screen.queryByText("注销账户")).toBeNull();
  });
});

describe("注销受理导航终态（T7 终审：冻结页独立路由 /account/deleting）", () => {
  const V2_ONLY_AUTH = {
    ...BASE_AUTH,
    v2User: { id: "u-2", email: "v2@example.com", role: "user", mfaEnabled: false },
  };

  /** 挂三路由（/profile + 冻结页 + /login 哨兵）渲染后走真实 DangerZone 受理流 */
  async function renderAndWalkDeletionFlow() {
    requestV2.mockResolvedValueOnce(ok([])); // 挂载会话列表
    render(
      <MemoryRouter initialEntries={["/profile"]}>
        <Routes>
          <Route path="/profile" element={<ProfilePage />} />
          <Route path="/account/deleting" element={<AccountDeletingPage />} />
          <Route path="/login" element={<div>登录页哨兵</div>} />
        </Routes>
      </MemoryRouter>
    );
    await act(async () => {});

    fireEvent.click(screen.getByRole("button", { name: "申请注销账户" }));
    await act(async () => {});
    fireEvent.change(screen.getByLabelText("登录密码"), {
      target: { value: "pw-123456" },
    });
    requestV2.mockResolvedValueOnce(ok({ status: "deleting", days_remaining: 14 }));
    fireEvent.click(screen.getByRole("button", { name: "确认注销" }));
    await act(async () => {});
  }

  it("危险区 200 → 清 V2 本地会话态 → 落在 /account/deleting，不被弹回 /login", async () => {
    useAuth.mockReturnValue(V2_ONLY_AUTH);
    await renderAndWalkDeletionFlow();

    // 本地 V2 态清理 + location 终态 = 冻结页路由（V2-only 用户匿名后
    // 仍可达——冻结页不挂 RequireAuth，「/login 哨兵」未被渲染）
    expect(useAuth().clearV2Session).toHaveBeenCalledTimes(1);
    expect(screen.getByText("账户注销中")).toBeTruthy();
    expect(screen.getByText(/14 天后生效/)).toBeTruthy();
    expect(screen.queryByText("登录页哨兵")).toBeNull();
    // 原页面卡片全部卸载（路由已切走）
    expect(screen.queryByText("设备与登录")).toBeNull();
    expect(screen.queryByText("专家身份")).toBeNull();
  });

  it("冻结页文案区分两个账户域（V1 工作区会话不受影响）", async () => {
    useAuth.mockReturnValue(V2_ONLY_AUTH);
    await renderAndWalkDeletionFlow();

    expect(screen.getByText(/旧版工作区账户/)).toBeTruthy();
    expect(screen.getByText(/不受影响/)).toBeTruthy();
  });
});
