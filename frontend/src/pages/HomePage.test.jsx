import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { request } from "../api/client.js";
import { requestV2 } from "../api/v2/client.js";
import { V2_DISCOVER, V2_TASKS } from "../api/v2/routes.js";
import HomePage from "./HomePage.jsx";

// 首页装配冒烟（Phase 8 T11）：双轨任务门控（V1 优先/否则 V2）+ discover 切 V2
// + V2-only 无全局错误横幅。身份源 mock useAuth；网络入口 request/requestV2 双 mock。
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/client.js", () => ({ request: vi.fn() }));
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

const V1_TASKS = {
  data: [
    {
      id: 1,
      status: "running",
      title: "季度数据盘点",
      expert_name_snapshot: "数据分析师",
    },
  ],
  total: 1,
};

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
  request.mockRejectedValue(new Error("V1 request：本用例未显式编排"));
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("V2-only 用户（简报 Step 2 ①：无错误横幅 + 任务列表渲染）", () => {
  it("任务列表走 V2_TASKS、discover 走 V2_DISCOVER，零 V1 调用、无 alert 横幅", async () => {
    useAuth.mockReturnValue({
      user: null,
      v2User: { id: "u-2", email: "v2@example.com", role: "user" },
      isExpert: false,
    });
    routeV2();
    renderHome();
    await flush();

    // V1 入口零调用（此前 V2-only 直调 /api/tasks 必 401 → 全局横幅的根源）
    expect(request).not.toHaveBeenCalled();
    const calledPaths = requestV2.mock.calls.map(([path]) => path);
    expect(calledPaths).toContain(`${V2_TASKS}?page=1&size=3`);
    expect(calledPaths).toContain(`${V2_DISCOVER}/experts?page=1&page_size=6`);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("V2 任务行渲染：§10.3 expert.name 作主行、provider.display_name 作辅行", async () => {
    useAuth.mockReturnValue({
      user: null,
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

describe("V1 会话用户（cutover 前双轨并存：V1 优先）", () => {
  it("任务列表走 V1 /api/tasks，discover 仍切 V2_DISCOVER", async () => {
    useAuth.mockReturnValue({
      user: { id: 1, username: "alice", email: "a@example.com", role: "user" },
      v2User: null,
      isExpert: false,
    });
    request.mockResolvedValue(V1_TASKS);
    routeV2();
    renderHome();
    await flush();

    expect(request).toHaveBeenCalledWith("/api/tasks?page=1&size=3");
    const calledPaths = requestV2.mock.calls.map(([path]) => path);
    expect(calledPaths).toContain(`${V2_DISCOVER}/experts?page=1&page_size=6`);
    // V1 行形状：title + expert_name_snapshot 直渲染
    expect(screen.getByText("季度数据盘点")).toBeTruthy();
    expect(screen.getByText("数据分析师")).toBeTruthy();
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
