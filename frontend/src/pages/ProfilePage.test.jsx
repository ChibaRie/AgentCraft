import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { requestV2 } from "../api/v2/client.js";
import { V2_TASKS } from "../api/v2/routes.js";
import AccountDeletingPage from "./AccountDeletingPage.jsx";
import ProfilePage from "./ProfilePage.jsx";

// 页面装配冒烟：身份/角色渲染源、安全区块 gating 与注销受理导航终态
// （卡片行为在各自组件测试覆盖；冻结页本体在 AccountDeletingPage.test）
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
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
  v2User: null,
  isExpert: false,
  refreshV2User: vi.fn(),
  clearV2Session: vi.fn(),
};

beforeEach(() => {
  vi.resetAllMocks();
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("角色渲染源（T2 账本 minor：V2 admin 曾显示 user）", () => {
  it("V2 会话 admin：角色行显示 admin + 管理员徽标", async () => {
    useAuth.mockReturnValue({
      ...BASE_AUTH,
      v2User: { id: "u-9", email: "root@example.com", role: "admin", status: "active" },
    });
    await renderProfile();

    expect(screen.getByText("admin")).toBeTruthy();
    expect(screen.getByText("管理员")).toBeTruthy();
    expect(screen.queryByText("普通用户")).toBeNull();
  });

  it("V2 会话普通用户：角色行 user + 普通用户徽标", async () => {
    useAuth.mockReturnValue({
      ...BASE_AUTH,
      v2User: { id: "u-2", email: "v2@example.com", role: "user", status: "active" },
    });
    await renderProfile();

    expect(screen.getByText("普通用户")).toBeTruthy();
  });
});

describe("安全区块 gating（端点均为 V2 会话语义）", () => {
  it("有 V2 会话：渲染设备/密码/两步验证/注销四卡片", async () => {
    useAuth.mockReturnValue({
      ...BASE_AUTH,
      v2User: { id: "u-2", email: "v2@example.com", role: "user", mfaEnabled: false },
    });
    await renderProfile();

    expect(screen.getByText("设备与登录")).toBeTruthy();
    expect(screen.getByText("密码修改")).toBeTruthy();
    expect(screen.getByText("两步验证")).toBeTruthy();
    expect(screen.getByText("注销账户")).toBeTruthy();
  });
  // 注：匿名态用例不设——/profile 挂 RequireAuth（无 V2 会话弹回 /login），
  // 匿名渲染在本页为不可达状态（T14 会话归一后 V1-only 用户域亦不存在）。
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

    // 本地 V2 态清理 + location 终态 = 冻结页路由（匿名到达不被弹回——
    // 冻结页不挂 RequireAuth，「/login 哨兵」未被渲染）
    expect(useAuth().clearV2Session).toHaveBeenCalledTimes(1);
    expect(screen.getByText("账户注销中")).toBeTruthy();
    expect(screen.getByText(/14 天后生效/)).toBeTruthy();
    expect(screen.queryByText("登录页哨兵")).toBeNull();
    // 原页面卡片全部卸载（路由已切走）
    expect(screen.queryByText("设备与登录")).toBeNull();
    expect(screen.queryByText("专家身份")).toBeNull();
  });

  it("冻结页文案区分两个账户域（旧版工作区账户独立域说明保留）", async () => {
    useAuth.mockReturnValue(V2_ONLY_AUTH);
    await renderAndWalkDeletionFlow();

    expect(screen.getByText(/旧版工作区账户/)).toBeTruthy();
    expect(screen.getByText(/不受影响/)).toBeTruthy();
  });
});

describe("任务列表（T14 收敛：唯一数据面 V2_TASKS）", () => {
  const V2_ONLY_USER = {
    ...BASE_AUTH,
    v2User: { id: "u-2", email: "v2@example.com", role: "user", mfaEnabled: false },
  };

  function routeV2(items) {
    requestV2.mockImplementation((path) => {
      if (path.startsWith(V2_TASKS)) {
        return Promise.resolve(ok({ items, total: items.length, page: 1, size: 20 }));
      }
      // 其余挂载卡片（SessionsCard 会话列表等）按各自测试语义回空
      return Promise.resolve(ok([]));
    });
  }

  it("任务列表走 V2_TASKS，§10.3 行形状渲染", async () => {
    routeV2([
      {
        id: "t-1",
        status: "running",
        created_at: "2026-09-15T00:00:00Z",
        expert: { name: "架构评审官", avatar_url: null },
        provider: { display_name: "gpt-4o", model: "gpt-4o" },
      },
    ]);
    useAuth.mockReturnValue(V2_ONLY_USER);
    await renderProfile();

    const taskCalls = requestV2.mock.calls.filter(([path]) => path.startsWith(V2_TASKS));
    expect(taskCalls).toHaveLength(1);
    expect(taskCalls[0][0]).toBe(`${V2_TASKS}?page=1&size=20`);
    // §10.3 行形状渲染：expert.name 主行 + 状态词表 + 创建时间（归一 createdAt）
    expect(screen.getByText("架构评审官")).toBeTruthy();
    expect(screen.getByText("进行中")).toBeTruthy();
    expect(screen.getByText(/gpt-4o · 2026/)).toBeTruthy();
  });
});

describe("专家身份说明（T11 ③/T14 收敛：无自助申请 CTA，entitlement 文案）", () => {
  it("非专家：无申请按钮，显示管理员授予说明", async () => {
    requestV2.mockResolvedValue(ok([]));
    useAuth.mockReturnValue({
      ...BASE_AUTH,
      v2User: { id: "u-2", email: "v2@example.com", role: "user", mfaEnabled: false },
    });
    await renderProfile();

    expect(screen.queryByRole("button", { name: "申请专家身份" })).toBeNull();
    expect(screen.getByText(/管理员授予/)).toBeTruthy();
  });

  it("entitlement 专家：已获身份文案（无需申请）", async () => {
    requestV2.mockResolvedValue(ok([]));
    useAuth.mockReturnValue({
      ...BASE_AUTH,
      v2User: {
        id: "u-2",
        email: "v2@example.com",
        role: "user",
        mfaEnabled: false,
        entitlements: ["expert_author"],
      },
      isExpert: true,
    });
    await renderProfile();

    expect(screen.getByText(/你已是专家用户/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "申请专家身份" })).toBeNull();
  });
});
