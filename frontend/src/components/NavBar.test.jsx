import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2_SESSION_EXPIRED_EVENT } from "../api/v2/client.js";
import NavBar from "./NavBar.jsx";

// NavBar 装配测试：mock useAuth 与 useNavigate（导航观察点）；Link/NavLink 用真实实现
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useNavigate: vi.fn() };
});

const navigateMock = vi.fn();

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定） */
function flush() {
  return act(async () => {});
}

const V1_USER = { id: 1, username: "alice", email: "a@example.com", role: "user" };
// email 前缀同为 alice——UserMenu 触发器可按名统一查找（V2 无 username 时取前缀）
const V2_USER = { id: "u-2", email: "alice@v2.example.com", role: "user", status: "active" };

/** 渲染 + 打开右上角 UserMenu 面板 */
async function openMenu() {
  render(
    <MemoryRouter>
      <NavBar />
    </MemoryRouter>
  );
  fireEvent.click(screen.getByRole("button", { name: /alice/ }));
  await flush();
}

function mockSession({ user, v2User, logoutV2, isExpert }) {
  useAuth.mockReturnValue({
    user,
    v2User,
    isAuthenticated: Boolean(user),
    // 上下文汇流值可显式注入（T11 ④：V2 entitlement 分支）；缺省按 V1 role 推导
    isExpert: isExpert ?? user?.role === "expert",
    logout: vi.fn(),
    logoutV2,
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  navigateMock.mockClear();
  useNavigate.mockReturnValue(navigateMock);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("「退出账户会话」入口 gating", () => {
  it("仅 V1 会话：不渲染该入口（V2 登出仅对 V2 会话有意义），V1「登出」保持", async () => {
    mockSession({ user: V1_USER, v2User: null, logoutV2: vi.fn() });
    await openMenu();

    // UserMenu 面板内条目均带显式 role="menuitem"
    expect(screen.queryByRole("menuitem", { name: "退出账户会话" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "登出" })).toBeTruthy();
  });

  it("V2 会话（含双轨）：入口出现在「登出」旁", async () => {
    mockSession({ user: V1_USER, v2User: V2_USER, logoutV2: vi.fn() });
    await openMenu();

    expect(screen.getByRole("menuitem", { name: "退出账户会话" })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: "登出" })).toBeTruthy();
  });
});

describe("任务域入口不再置灰（T11 ⑤：任务面即将全 V2）", () => {
  it("无任何会话：三个任务域链接照常渲染，无 aria-disabled / 提示 / 拦截", () => {
    mockSession({ user: null, v2User: null, logoutV2: vi.fn() });
    render(
      <MemoryRouter>
        <NavBar />
      </MemoryRouter>
    );

    for (const label of ["专家中心", "任务", "技能管理"]) {
      const link = screen.getByRole("link", { name: label });
      expect(link.getAttribute("aria-disabled")).toBeNull();
      expect(link.getAttribute("title")).toBeNull();
      expect(link.style.pointerEvents).toBe("");
    }
    // 无会话仍保留普通登录引导
    expect(screen.getByRole("link", { name: "登录" })).toBeTruthy();
  });
});

describe("isExpert 汇流：徽标与菜单统一（T11 ④）", () => {
  it("V2-only expert_author（entitlement 分支）：菜单含我的专家，徽标显示专家", async () => {
    mockSession({
      user: null,
      v2User: { ...V2_USER, entitlements: ["expert_author"] },
      logoutV2: vi.fn(),
      isExpert: true,
    });
    await openMenu();

    expect(screen.getByRole("menuitem", { name: "我的专家" })).toBeTruthy();
    expect(screen.getByText("专家")).toBeTruthy();
  });

  it("V2-only 非 expert：菜单无我的专家、无专家徽标", async () => {
    mockSession({ user: null, v2User: V2_USER, logoutV2: vi.fn() });
    await openMenu();

    expect(screen.queryByRole("menuitem", { name: "我的专家" })).toBeNull();
    expect(screen.queryByText("专家")).toBeNull();
  });
});

describe("退出账户会话流（logout 导航收敛规则）", () => {
  it("200 路径：logoutV2 调用一次 → 调用方 navigate /login", async () => {
    const logoutV2 = vi.fn().mockResolvedValue(undefined);
    mockSession({ user: null, v2User: V2_USER, logoutV2 });
    await openMenu();

    fireEvent.click(screen.getByRole("menuitem", { name: "退出账户会话" }));
    await flush();

    expect(logoutV2).toHaveBeenCalledTimes(1);
    expect(navigateMock).toHaveBeenCalledWith("/login");
  });

  it("401 路径（死 cookie 重放）：事件已跳 /login?v2=1 → 不二次跳转、无错误上抛", async () => {
    // 复现生产链路：requestV2 401 派发事件 → AuthProvider 订阅跳转；logoutV2 收敛 resolve
    const logoutV2 = vi.fn(async () => {
      window.dispatchEvent(
        new CustomEvent(V2_SESSION_EXPIRED_EVENT, { detail: { path: "/api/v2/auth/logout" } })
      );
    });
    mockSession({ user: null, v2User: V2_USER, logoutV2 });
    await openMenu();

    fireEvent.click(screen.getByRole("menuitem", { name: "退出账户会话" }));
    await flush();

    expect(logoutV2).toHaveBeenCalledTimes(1);
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("登出窗口之外的事件不干扰判断（监听随 await 窗口装卸）", async () => {
    const logoutV2 = vi.fn().mockResolvedValue(undefined);
    mockSession({ user: null, v2User: V2_USER, logoutV2 });
    await openMenu();

    // 点击前到达的无关 401 事件（其他请求失效）不命中登出监听
    act(() => {
      window.dispatchEvent(new CustomEvent(V2_SESSION_EXPIRED_EVENT, { detail: {} }));
    });
    fireEvent.click(screen.getByRole("menuitem", { name: "退出账户会话" }));
    await flush();

    expect(logoutV2).toHaveBeenCalledTimes(1);
    expect(navigateMock).toHaveBeenCalledWith("/login");
  });
});
