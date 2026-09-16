import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2_SESSION_EXPIRED_EVENT } from "../api/v2/client.js";
import NavBar from "./NavBar.jsx";

// NavBar 装配测试（T14 会话归一：唯一会话域 V2）：mock useAuth 与 useNavigate
// （导航观察点）；Link/NavLink 用真实实现。
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

// email 前缀为 alice——UserMenu 触发器可按名统一查找（V2 无 username 时取前缀）
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

function mockSession({ v2User, logoutV2, isExpert }) {
  useAuth.mockReturnValue({
    v2User,
    isExpert: isExpert ?? false,
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

describe("会话引导（T14 归一：唯一会话域）", () => {
  it("匿名：渲染「登录」引导链接，无 UserMenu", () => {
    mockSession({ v2User: null, logoutV2: vi.fn() });
    render(
      <MemoryRouter>
        <NavBar />
      </MemoryRouter>
    );

    expect(screen.getByRole("link", { name: "登录" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /alice/ })).toBeNull();
    // 三个任务域链接照常渲染，无置灰降级（T11 ⑤）
    for (const label of ["专家中心", "任务", "技能管理"]) {
      const link = screen.getByRole("link", { name: label });
      expect(link.getAttribute("aria-disabled")).toBeNull();
      expect(link.getAttribute("title")).toBeNull();
      expect(link.style.pointerEvents).toBe("");
    }
  });

  it("V2 会话：「退出账户会话」为唯一登出入口，V1「登出」条目已随轨删除", async () => {
    mockSession({ v2User: V2_USER, logoutV2: vi.fn() });
    await openMenu();

    expect(screen.getByRole("menuitem", { name: "退出账户会话" })).toBeTruthy();
    expect(screen.queryByRole("menuitem", { name: "登出" })).toBeNull();
  });
});

describe("isExpert 单判据：徽标与菜单统一（T14 收敛）", () => {
  it("V2 expert_author（entitlement）：菜单含我的专家，徽标显示专家", async () => {
    mockSession({
      v2User: { ...V2_USER, entitlements: ["expert_author"] },
      logoutV2: vi.fn(),
      isExpert: true,
    });
    await openMenu();

    expect(screen.getByRole("menuitem", { name: "我的专家" })).toBeTruthy();
    expect(screen.getByText("专家")).toBeTruthy();
  });

  it("V2 非 expert：菜单无我的专家、无专家徽标", async () => {
    mockSession({ v2User: V2_USER, logoutV2: vi.fn() });
    await openMenu();

    expect(screen.queryByRole("menuitem", { name: "我的专家" })).toBeNull();
    expect(screen.queryByText("专家")).toBeNull();
  });
});

describe("退出账户会话流（logout 导航收敛规则）", () => {
  it("200 路径：logoutV2 调用一次 → 调用方 navigate /login", async () => {
    const logoutV2 = vi.fn().mockResolvedValue(undefined);
    mockSession({ v2User: V2_USER, logoutV2 });
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
        new CustomEvent(V2_SESSION_EXPIRED_EVENT, { detail: { path: "/api/auth/logout" } })
      );
    });
    mockSession({ v2User: V2_USER, logoutV2 });
    await openMenu();

    fireEvent.click(screen.getByRole("menuitem", { name: "退出账户会话" }));
    await flush();

    expect(logoutV2).toHaveBeenCalledTimes(1);
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("登出窗口之外的事件不干扰判断（监听随 await 窗口装卸）", async () => {
    const logoutV2 = vi.fn().mockResolvedValue(undefined);
    mockSession({ v2User: V2_USER, logoutV2 });
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
