import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { requestV2 } from "../api/v2/client.js";
import { V2_AUTHORING_SKILLS, V2_DISCOVER, V2_TASKS } from "../api/v2/routes.js";
import HomePage from "./HomePage.jsx";

// 首页装配冒烟（Phase 8 T14 会话归一）：任务/discover/我的 Skill 全数据面走 V2。
// 身份源 mock useAuth；网络入口 requestV2 单 mock。
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

function renderHome() {
  return render(
    <MemoryRouter>
      <HomePage />
    </MemoryRouter>
  );
}

/** 排空微任务（两个 effect 的异步 load 全部落定） */
async function flush() {
  await act(async () => {});
}

const V2_TASKS_PAGE = {
  items: [
    {
      id: "t-1",
      status: "running",
      created_at: "2026-09-15T00:00:00Z",
      expert: { name: "架构评审官", avatar_url: null },
      provider: { display_name: "gpt-4o", model: "gpt-4o" },
    },
  ],
  total: 1,
  page: 1,
  size: 3,
};

const V2_DISCOVER_PAGE = {
  items: [
    {
      id: "e-1",
      published_revision_id: "rev-1",
      // 刻意与任务行的 expert.name 不同值——两区渲染互不混淆
      name: "文案顾问",
      description: "评审系统设计",
      category: "engineering",
      skill_count: 2,
    },
  ],
  total: 7,
  page: 1,
  page_size: 6,
};

function routeV2(tasksPage = V2_TASKS_PAGE, discoverPage = V2_DISCOVER_PAGE) {
  requestV2.mockImplementation((path) => {
    if (path.startsWith(V2_TASKS)) {
      return Promise.resolve({ status: 200, data: tasksPage, headers: new Headers() });
    }
    if (path.startsWith(V2_DISCOVER)) {
      return Promise.resolve({ status: 200, data: discoverPage, headers: new Headers() });
    }
    return Promise.reject(new Error(`未编排的 requestV2 调用：${path}`));
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("V2 会话用户（T14 归一：无错误横幅 + 任务列表渲染）", () => {
  it("任务列表走 V2_TASKS、discover 走 V2_DISCOVER，无 alert 横幅", async () => {
    useAuth.mockReturnValue({
      v2User: { id: "u-2", email: "v2@example.com", role: "user" },
      isExpert: false,
    });
    routeV2();
    renderHome();
    await flush();

    const calledPaths = requestV2.mock.calls.map(([path]) => path);
    expect(calledPaths).toContain(`${V2_TASKS}?page=1&size=3`);
    expect(calledPaths).toContain(`${V2_DISCOVER}/experts?page=1&page_size=6`);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("V2 任务行渲染：§10.3 expert.name 作主行、provider.display_name 作辅行", async () => {
    useAuth.mockReturnValue({
      v2User: { id: "u-2", email: "v2@example.com", role: "user" },
      isExpert: false,
    });
    routeV2();
    renderHome();
    await flush();

    expect(screen.getByText("架构评审官")).toBeTruthy();
    expect(screen.getByText("gpt-4o")).toBeTruthy();
    expect(screen.getByText("进行中")).toBeTruthy();
    // discover 区独立渲染（name 与任务行不同值）
    expect(screen.getByText("文案顾问")).toBeTruthy();
  });
});

describe("专家用户工作台速览（T14 收敛：我的 Skill 走 V2 作者面）", () => {
  it("isExpert → V2_AUTHORING_SKILLS 取数渲染 mySkills 计数", async () => {
    useAuth.mockReturnValue({
      v2User: { id: "u-2", email: "v2@example.com", role: "user" },
      isExpert: true,
    });
    requestV2.mockImplementation((path) => {
      if (path.startsWith(V2_TASKS)) {
        return Promise.resolve({ status: 200, data: V2_TASKS_PAGE, headers: new Headers() });
      }
      if (path.startsWith(V2_DISCOVER)) {
        return Promise.resolve({ status: 200, data: V2_DISCOVER_PAGE, headers: new Headers() });
      }
      if (path.startsWith(V2_AUTHORING_SKILLS)) {
        return Promise.resolve({
          status: 200,
          data: { items: [], total: 4, page: 1, size: 1 },
          headers: new Headers(),
        });
      }
      return Promise.reject(new Error(`未编排的 requestV2 调用：${path}`));
    });
    renderHome();
    await flush();

    const calledPaths = requestV2.mock.calls.map(([path]) => path);
    expect(calledPaths).toContain(`${V2_AUTHORING_SKILLS}?page=1&size=1`);
    expect(screen.getByText("4")).toBeTruthy();
    // 匿名/非专家不发起作者面取数
    expect(calledPaths.filter((path) => path.startsWith(V2_AUTHORING_SKILLS))).toHaveLength(1);
  });

  it("非专家：不发起 V2_AUTHORING_SKILLS 调用，mySkills 保持占位", async () => {
    useAuth.mockReturnValue({
      v2User: { id: "u-2", email: "v2@example.com", role: "user" },
      isExpert: false,
    });
    routeV2();
    renderHome();
    await flush();

    const calledPaths = requestV2.mock.calls.map(([path]) => path);
    expect(calledPaths.some((path) => path.startsWith(V2_AUTHORING_SKILLS))).toBe(false);
    expect(screen.getByText("—")).toBeTruthy();
  });
});

describe("精选专家（discover 切 V2，信封 {items,total}）", () => {
  it("渲染 V2 列表项与总数", async () => {
    useAuth.mockReturnValue({
      user: null,
      v2User: { id: "u-2", email: "v2@example.com", role: "user" },
      isExpert: false,
    });
    routeV2();
    renderHome();
    await flush();

    expect(screen.getByText("文案顾问")).toBeTruthy();
    expect(screen.getByText(/专家中心共有 7 位公开专家/)).toBeTruthy();
  });
});
