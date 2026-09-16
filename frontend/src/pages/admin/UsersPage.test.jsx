import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../../auth/AuthContext.jsx";
import { requestV2, V2ApiError } from "../../api/v2/client.js";
import RequireAdmin from "../../components/RequireAdmin.jsx";
import { ADMIN_DOMAIN_409_COPY, IDEMPOTENCY_CONFLICT_COPY } from "../../components/AdminReasonPrompt.jsx";
import UsersPage from "./UsersPage.jsx";

// 用户管理页（Phase 8 T12a，测试清单⑤⑥⑧）：suspend cascade 五计数回执、
// 重放基线 cascade=null 非告警（R2）、entitlements DELETE 带 JSON body（D4a）、
// 域 409 按码分流（安全 Minor 3）、配额 strict int 表单、D8 用户任务列表。
// 页面经 RequireAdmin 挂载（真实接线——useAdminGate 上下文由此提供）。
vi.mock("../../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

const ADMIN_CTX = {
  authReady: true,
  v2User: { id: "u-admin", email: "admin@example.com", role: "admin", status: "active", mfaEnabled: true },
  refreshV2User: vi.fn(),
};

function renderPage() {
  return render(
    <MemoryRouter>
      <RequireAdmin>
        <UsersPage />
      </RequireAdmin>
    </MemoryRouter>
  );
}

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

function flush() {
  return act(async () => {});
}

/** 按「METHOD path」分派的可编程打桩 */
function stubByPath(routes) {
  requestV2.mockImplementation((path, options = {}) => {
    const key = `${options.method || "GET"} ${path}`;
    const handler = routes[key];
    if (!handler) {
      return Promise.reject(new Error(`未编排的请求：${key}`));
    }
    return Promise.resolve(handler(options));
  });
}

const USER_A = {
  id: "u1",
  email: "alice@example.com",
  role: "user",
  status: "active",
  created_at: "2026-09-01T08:00:00",
};
const LIST_PAGE = { items: [USER_A], total: 1, page: 1, size: 20 };
const DETAIL = {
  user: USER_A,
  quotas: {
    max_daily_tasks: 30,
    max_active_tasks: 5,
    max_running_tasks: 2,
    max_retained_storage_bytes: 1073741824,
  },
  usage: { max_daily_tasks: 3, max_active_tasks: 1, max_running_tasks: 0, max_retained_storage_bytes: 2048 },
  tasks: { total: 7, by_status: { running: 1, done: 6 } },
};
const CASCADE_FULL = {
  sessions_revoked: 2,
  tokens_invalidated: 3,
  flipped_tasks: 1,
  cancelled_rounds: 0,
  stopped: 1,
};

const REASON_LABEL = "操作原因（必填，审计留痕）";

/** 装载列表 → 打开 u1 详情抽屉 */
async function openDrawer(routes) {
  stubByPath(routes);
  renderPage();
  await flush();
  fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
  await flush();
  return within(screen.getByRole("dialog"));
}

async function confirmPrompt() {
  fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "运营处置" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
  });
  await flush();
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue(ADMIN_CTX);
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("列表与过滤", () => {
  it("挂载拉取用户列表；email_prefix/status 过滤进查询串", async () => {
    stubByPath({ "GET /api/admin/users": () => ok(LIST_PAGE) });
    renderPage();
    await flush();

    expect(requestV2).toHaveBeenCalledWith("/api/admin/users", expect.anything());
    expect(screen.getByText("alice@example.com")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("邮箱前缀"), { target: { value: "ali" } });
    fireEvent.change(screen.getByLabelText("状态过滤"), { target: { value: "suspended" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/users?email_prefix=ali&status=suspended",
      expect.anything()
    );
  });

  it("latest-call 守卫：旧查询后返回时被丢弃，列表保持最新一次结果（Phase 9 T7）", async () => {
    const OTHER = { ...USER_A, email: "bob@example.com" };
    let releaseFirst;
    const firstPending = new Promise((resolve) => {
      releaseFirst = () => resolve(ok({ items: [USER_A], total: 1, page: 1, size: 20 }));
    });
    let call = 0;
    requestV2.mockImplementation(() => {
      call += 1;
      if (call === 1) {
        return firstPending;
      }
      return Promise.resolve(ok({ items: [OTHER], total: 1, page: 1, size: 20 }));
    });

    renderPage();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await flush();
    expect(screen.getByText("bob@example.com")).toBeTruthy();

    // 旧查询随后返回：守卫应丢弃，不回滚为旧结果
    await act(async () => {
      releaseFirst();
      await Promise.resolve();
    });
    await flush();
    expect(screen.getByText("bob@example.com")).toBeTruthy();
    expect(screen.queryByText("alice@example.com")).toBeNull();
  });
});

describe("详情抽屉四区块", () => {
  it("首开即时渲染抽屉并在详情未落定时显示加载反馈（Phase 9 T7）", async () => {
    let releaseDetail;
    const pendingDetail = new Promise((resolve) => {
      releaseDetail = () => resolve(ok(DETAIL));
    });
    requestV2.mockImplementation((path) => {
      // 列表路径恰为 /api/admin/users（可带查询串）；详情为 /api/admin/users/{id}
      if (String(path).split("?")[0] === "/api/admin/users") {
        return Promise.resolve(ok(LIST_PAGE));
      }
      return pendingDetail;
    });

    renderPage();
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
    await flush();

    // 详情尚未返回：抽屉已开、显示加载中、且数据依赖的操作入口未渲染
    const drawer = within(screen.getByRole("dialog"));
    expect(drawer.getByText("加载中…")).toBeTruthy();
    expect(drawer.getByText(/alice@example\.com/)).toBeTruthy(); // 目标行来自列表
    expect(drawer.queryByRole("button", { name: "停用账户" })).toBeNull();

    await act(async () => {
      releaseDetail();
      await Promise.resolve();
    });
    await flush();
    expect(drawer.getByRole("button", { name: "停用账户" })).toBeTruthy();
  });

  it("打开抽屉拉取详情：概览/配额/权限/任务计数渲染", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () => ok(DETAIL),
    });

    expect(drawer.getByText(/alice@example\.com/)).toBeTruthy();
    expect(drawer.getByLabelText("每日任务上限").value).toBe("30");
    expect(drawer.getByLabelText("活跃任务上限").value).toBe("5");
    expect(drawer.getByLabelText("并发任务上限").value).toBe("2");
    expect(drawer.getByLabelText("保留存储上限（字节）").value).toBe("1073741824");
    expect(drawer.getByRole("button", { name: "授予作者权限" })).toBeTruthy();
    expect(drawer.getByRole("button", { name: "撤销作者权限" })).toBeTruthy();
    expect(drawer.getByRole("button", { name: "停用账户" })).toBeTruthy();
    expect(drawer.getByText(/任务总数/)).toBeTruthy();
  });

  it("D8 用户任务列表：查看任务列表 → GET /users/{id}/tasks 元数据读", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () => ok(DETAIL),
      "GET /api/admin/users/u1/tasks": () =>
        ok({ items: [{ id: "t1", status: "running", abort_reason: null, created_at: "2026-09-12T08:00:00" }], total: 1, page: 1, size: 20 }),
    });

    fireEvent.click(drawer.getByRole("button", { name: "查看任务列表" }));
    await flush();

    expect(requestV2).toHaveBeenCalledWith("/api/admin/users/u1/tasks", expect.anything());
    expect(drawer.getByText("t1")).toBeTruthy();
    expect(drawer.getByText(/running/)).toBeTruthy();
  });
});

describe("停用与级联回执（⑤）", () => {
  it("suspend 成功 → cascade 五计数回执渲染", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () => ok(DETAIL),
      "POST /api/admin/users/u1/suspend": () =>
        ok({
          user_id: "u1",
          before_status: "active",
          status: "suspended",
          deadline_cleared: false,
          cascade: CASCADE_FULL,
        }),
    });

    fireEvent.click(drawer.getByRole("button", { name: "停用账户" }));
    await flush();
    await confirmPrompt();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/users/u1/suspend",
      expect.objectContaining({
        method: "POST",
        body: { reason: "运营处置" },
        idempotencyKey: expect.any(String),
      })
    );
    const receipt = within(screen.getByRole("dialog")).getByText(/最近操作回执/).closest("section");
    expect(receipt.textContent).toContain("active → suspended");
    expect(receipt.textContent).toContain("已撤销会话：2");
    expect(receipt.textContent).toContain("已置废令牌：3");
    expect(receipt.textContent).toContain("翻转任务：1");
    expect(receipt.textContent).toContain("取消轮次：0");
    expect(receipt.textContent).toContain("停止执行：1");
  });

  it("重放基线 cascade=null → 「以提交时刻基线为准」提示，不渲染级联失败告警（R2）", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () => ok(DETAIL),
      "POST /api/admin/users/u1/suspend": () =>
        ok({
          user_id: "u1",
          before_status: "active",
          status: "suspended",
          deadline_cleared: false,
          cascade: null,
        }),
    });

    fireEvent.click(drawer.getByRole("button", { name: "停用账户" }));
    await flush();
    await confirmPrompt();

    const receiptArea = screen.getByRole("dialog").textContent;
    expect(receiptArea).toContain("以提交时刻基线为准");
    expect(receiptArea).not.toContain("级联失败");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("unsuspend 成功 → POST /users/{id}/unsuspend；恢复按钮在 suspended 态出现", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () =>
        ok({ ...DETAIL, user: { ...USER_A, status: "suspended" } }),
      "POST /api/admin/users/u1/unsuspend": () =>
        ok({ user_id: "u1", before_status: "suspended", status: "active" }),
    });

    expect(drawer.queryByRole("button", { name: "停用账户" })).toBeNull();
    fireEvent.click(drawer.getByRole("button", { name: "恢复账户" }));
    await flush();
    await confirmPrompt();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/users/u1/unsuspend",
      expect.objectContaining({ method: "POST", body: { reason: "运营处置" } })
    );
  });

  it("域 409 USER_STATUS_CONFLICT → 域文案，不走幂等冲突文案（⑧）", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () => ok(DETAIL),
      "POST /api/admin/users/u1/suspend": () =>
        Promise.reject(new V2ApiError("USER_STATUS_CONFLICT", "用户状态已变化", 409)),
    });

    fireEvent.click(drawer.getByRole("button", { name: "停用账户" }));
    await flush();
    await confirmPrompt();

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toBe(ADMIN_DOMAIN_409_COPY.USER_STATUS_CONFLICT);
    expect(alert.textContent).not.toBe(IDEMPOTENCY_CONFLICT_COPY);
  });
});

describe("entitlements 授/撤（⑥ D4a）", () => {
  it("撤销：DELETE 带 JSON body {kind, reason} + 幂等键", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () => ok(DETAIL),
      "DELETE /api/admin/users/u1/entitlements": () =>
        ok({ user_id: "u1", entitlement: "expert_author", entitlement_id: "e1", revoked_at: "2026-09-12T08:00:00" }),
    });

    fireEvent.click(drawer.getByRole("button", { name: "撤销作者权限" }));
    await flush();
    await confirmPrompt();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/users/u1/entitlements",
      expect.objectContaining({
        method: "DELETE",
        body: { kind: "expert_author", reason: "运营处置" },
        idempotencyKey: expect.any(String),
      })
    );
  });

  it("授予：POST 载荷 {kind, reason}；409 ENTITLEMENT_ACTIVE 走域文案", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () => ok(DETAIL),
      "POST /api/admin/users/u1/entitlements": () =>
        Promise.reject(new V2ApiError("ENTITLEMENT_ACTIVE", "已持有", 409)),
    });

    fireEvent.click(drawer.getByRole("button", { name: "授予作者权限" }));
    await flush();
    await confirmPrompt();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/users/u1/entitlements",
      expect.objectContaining({
        method: "POST",
        body: { kind: "expert_author", reason: "运营处置" },
        idempotencyKey: expect.any(String),
      })
    );
    expect(screen.getByRole("alert").textContent).toBe(ADMIN_DOMAIN_409_COPY.ENTITLEMENT_ACTIVE);
  });
});

describe("配额调整（strict int）", () => {
  it("仅提交填写的维度；PUT /users/{id}/quotas 载荷 snake_case + reason", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () => ok(DETAIL),
      "PUT /api/admin/users/u1/quotas": () =>
        ok({ user_id: "u1", quotas: { ...DETAIL.quotas, max_daily_tasks: 50 } }),
    });

    fireEvent.change(drawer.getByLabelText("每日任务上限"), { target: { value: "50" } });
    fireEvent.click(drawer.getByRole("button", { name: "保存配额" }));
    await flush();
    await confirmPrompt();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/users/u1/quotas",
      expect.objectContaining({
        method: "PUT",
        body: { max_daily_tasks: 50, reason: "运营处置" },
        idempotencyKey: expect.any(String),
      })
    );
  });

  it("非整数/负数输入 → 客户端拦截，不开弹窗不发请求", async () => {
    const drawer = await openDrawer({
      "GET /api/admin/users": () => ok(LIST_PAGE),
      "GET /api/admin/users/u1": () => ok(DETAIL),
    });

    fireEvent.change(drawer.getByLabelText("每日任务上限"), { target: { value: "-3" } });
    fireEvent.click(drawer.getByRole("button", { name: "保存配额" }));
    await flush();

    // 详情抽屉本身也是 dialog——用 reason 弹窗的特征控件断言「未开弹窗」
    expect(screen.queryByLabelText(REASON_LABEL)).toBeNull();
    expect(drawer.getByRole("alert").textContent).toContain("非负整数");
    expect(requestV2).not.toHaveBeenCalledWith(
      "/api/admin/users/u1/quotas",
      expect.anything()
    );
  });
});
