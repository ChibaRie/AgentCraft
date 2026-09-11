import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { request } from "../api/client.js";
import { requestV2 } from "../api/v2/client.js";
import ProfilePage from "./ProfilePage.jsx";

// 页面装配冒烟：身份/角色渲染源与安全区块 gating（卡片行为在各自组件测试覆盖）
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/client.js", () => ({ request: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

/** render + 排空微任务（任务列表 effect 异步落定，避免 act 告警） */
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
      user: null,
      v2User: { id: "u-9", email: "root@example.com", role: "admin", status: "active" },
      isExpert: false,
      applyExpert: vi.fn(),
      refreshV2User: vi.fn(),
    });
    await renderProfile();

    expect(screen.getByText("admin")).toBeTruthy();
    expect(screen.getByText("管理员")).toBeTruthy();
    expect(screen.queryByText("普通用户")).toBeNull();
  });

  it("V1 expert：专家徽标 + expert 角色行（V1 域行为回归钉死）", async () => {
    useAuth.mockReturnValue({
      user: { id: 1, username: "alice", email: "a@example.com", role: "expert" },
      v2User: null,
      isExpert: true,
      applyExpert: vi.fn(),
      refreshV2User: vi.fn(),
    });
    await renderProfile();

    expect(screen.getByText("专家")).toBeTruthy();
    expect(screen.getByText("expert")).toBeTruthy();
  });
});

describe("安全区块 gating（端点均为 V2 会话语义）", () => {
  it("有 V2 会话（含双轨）：渲染密码修改与两步验证两卡片", async () => {
    useAuth.mockReturnValue({
      user: { id: 1, username: "alice", email: "a@example.com", role: "user" },
      v2User: { id: "u-2", email: "v2@example.com", role: "user", mfaEnabled: false },
      isExpert: false,
      applyExpert: vi.fn(),
      refreshV2User: vi.fn(),
    });
    await renderProfile();

    expect(screen.getByText("密码修改")).toBeTruthy();
    expect(screen.getByText("两步验证")).toBeTruthy();
  });

  it("仅 V1 会话：不渲染安全区块（避免对无 V2 会话者暴露必 401 的操作面）", async () => {
    useAuth.mockReturnValue({
      user: { id: 1, username: "alice", email: "a@example.com", role: "user" },
      v2User: null,
      isExpert: false,
      applyExpert: vi.fn(),
      refreshV2User: vi.fn(),
    });
    await renderProfile();

    expect(screen.queryByText("密码修改")).toBeNull();
    expect(screen.queryByText("两步验证")).toBeNull();
  });
});
