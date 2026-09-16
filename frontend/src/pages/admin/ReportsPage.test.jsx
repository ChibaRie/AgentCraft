import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../../auth/AuthContext.jsx";
import { requestV2 } from "../../api/v2/client.js";
import RequireAdmin from "../../components/RequireAdmin.jsx";
import ReportsPage from "./ReportsPage.jsx";

// 举报队列页（Phase 8 T12b，测试清单②⑤）：action 三选一（message 目标禁用
// ban/takedown 前端预判）；ban_author 成功展示 suspension+cascade（null 非告警
// R2）；「查看上下文」→ D8 tasks-read 抽屉（reason 弹窗先审计后读提示语——
// 未提交 reason 前零读取请求）。页面经 RequireAdmin 挂载（真实接线）。
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
        <ReportsPage />
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

/** 按「METHOD path」分派的可编程打桩（查询串剥离后匹配；真实查询串在断言侧核对） */
function stubByPath(routes) {
  requestV2.mockImplementation((path, options = {}) => {
    const key = `${options.method || "GET"} ${String(path).split("?")[0]}`;
    const handler = routes[key];
    if (!handler) {
      return Promise.reject(new Error(`未编排的请求：${key}`));
    }
    return Promise.resolve(handler(options));
  });
}

const REPORT_MSG = {
  id: "rp-msg",
  target_type: "message",
  target_id: "m-1",
  status: "open",
  reason: "回复内容包含诱导指令",
  created_at: "2026-09-14T08:00:00",
};

const REPORT_EXPERT = {
  id: "rp-exp",
  target_type: "expert_revision",
  target_id: "rev-9",
  status: "open",
  reason: "专家描述与实际行为不符",
  created_at: "2026-09-14T09:00:00",
};

const REASON_LABEL = "操作原因（必填，审计留痕）";

function stubQueueOnly(report) {
  stubByPath({
    "GET /api/admin/reports": () => ok({ items: [report], total: 1, page: 1, size: 20 }),
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue(ADMIN_CTX);
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("队列装载", () => {
  it("挂载拉取 open 举报队列", async () => {
    stubQueueOnly(REPORT_MSG);
    renderPage();
    await flush();

    expect(requestV2).toHaveBeenCalledWith("/api/admin/reports", expect.anything());
    expect(screen.getByText("rp-msg")).toBeTruthy();
    expect(screen.getByText(/诱导指令/)).toBeTruthy();
  });
});

describe("②处置 action 三选一（message 目标禁用 ban/takedown）", () => {
  async function openChooser(report) {
    stubQueueOnly(report);
    renderPage();
    await flush();
    const row = screen.getByText(report.id).closest("tr");
    fireEvent.click(within(row).getByRole("button", { name: "处置" }));
    await flush();
    return within(screen.getByRole("dialog"));
  }

  it("expert_revision 目标：三选项可用", async () => {
    const chooser = await openChooser(REPORT_EXPERT);
    const takedown = chooser.getByRole("radio", { name: /下架当前发布版本/ });
    const ban = chooser.getByRole("radio", { name: /封禁作者/ });
    expect(takedown.disabled).toBe(false);
    expect(ban.disabled).toBe(false);
    expect(chooser.getByRole("radio", { name: /驳回举报/ }).disabled).toBe(false);
  });

  it("message 目标：ban_author 与 takedown_revision 前端禁用（预判，零 resolve 请求）", async () => {
    const chooser = await openChooser(REPORT_MSG);
    const takedown = chooser.getByRole("radio", { name: /下架当前发布版本/ });
    const ban = chooser.getByRole("radio", { name: /封禁作者/ });
    expect(takedown.disabled).toBe(true);
    expect(ban.disabled).toBe(true);
    expect(chooser.getByRole("radio", { name: /驳回举报/ }).disabled).toBe(false);
  });

  it("resolve dismiss：经 reason 弹窗提交 POST {action, reason}", async () => {
    stubByPath({
      "GET /api/admin/reports": () => ok({ items: [REPORT_MSG], total: 1, page: 1, size: 20 }),
      "POST /api/admin/reports/rp-msg/resolve": () =>
        ok({ report_id: "rp-msg", status: "dismissed" }),
    });
    renderPage();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "处置" }));
    const chooser = within(screen.getByRole("dialog"));
    fireEvent.click(chooser.getByRole("radio", { name: /驳回举报/ }));
    fireEvent.click(chooser.getByRole("button", { name: "下一步" }));
    await flush();

    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "运营处置" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    });
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/reports/rp-msg/resolve",
      expect.objectContaining({
        method: "POST",
        body: { action: "dismiss", reason: "运营处置" },
        idempotencyKey: expect.any(String),
      })
    );
    expect(screen.getByText(/最近处置回执/)).toBeTruthy();
    expect(screen.getByText(/dismissed/)).toBeTruthy();
  });

  it("ban_author：suspension+cascade 五计数回执渲染", async () => {
    stubByPath({
      "GET /api/admin/reports": () => ok({ items: [REPORT_EXPERT], total: 1, page: 1, size: 20 }),
      "POST /api/admin/reports/rp-exp/resolve": () =>
        ok({
          report_id: "rp-exp",
          status: "actioned",
          banned_user_id: "u9",
          suspension: { user_id: "u9", before_status: "active", status: "suspended", deadline_cleared: false },
          cascade: { sessions_revoked: 2, tokens_invalidated: 3, flipped_tasks: 1, cancelled_rounds: 0, stopped: 1 },
        }),
    });
    renderPage();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "处置" }));
    const chooser = within(screen.getByRole("dialog"));
    fireEvent.click(chooser.getByRole("radio", { name: /封禁作者/ }));
    fireEvent.click(chooser.getByRole("button", { name: "下一步" }));
    await flush();

    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "运营处置" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    });
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/reports/rp-exp/resolve",
      expect.objectContaining({
        method: "POST",
        body: { action: "ban_author", reason: "运营处置" },
        idempotencyKey: expect.any(String),
      })
    );
    expect(screen.getByText(/最近处置回执/)).toBeTruthy();
    expect(screen.getByText(/active → suspended/)).toBeTruthy();
    expect(screen.getByText(/已撤销会话：2/)).toBeTruthy();
    expect(screen.getByText(/已置废令牌：3/)).toBeTruthy();
    expect(screen.getByText(/翻转任务：1/)).toBeTruthy();
    expect(screen.getByText(/取消轮次：0/)).toBeTruthy();
    expect(screen.getByText(/停止执行：1/)).toBeTruthy();
  });

  it("ban_author 重放基线 cascade=null → 非告警提示（R2）", async () => {
    stubByPath({
      "GET /api/admin/reports": () => ok({ items: [REPORT_EXPERT], total: 1, page: 1, size: 20 }),
      "POST /api/admin/reports/rp-exp/resolve": () =>
        ok({
          report_id: "rp-exp",
          status: "actioned",
          banned_user_id: "u9",
          suspension: { user_id: "u9", before_status: "active", status: "suspended", deadline_cleared: false },
          cascade: null,
        }),
    });
    renderPage();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "处置" }));
    const chooser = within(screen.getByRole("dialog"));
    fireEvent.click(chooser.getByRole("radio", { name: /封禁作者/ }));
    fireEvent.click(chooser.getByRole("button", { name: "下一步" }));
    await flush();

    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "运营处置" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    });
    await flush();

    expect(screen.getByText(/以提交时刻基线为准/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("⑤「查看上下文」→ D8 抽屉（reason 前置）", () => {
  it("reason 弹窗（先审计后读提示语）先于读取；提交后 messages/files 带原因查询串", async () => {
    stubByPath({
      "GET /api/admin/reports": () => ok({ items: [REPORT_MSG], total: 1, page: 1, size: 20 }),
      "GET /api/admin/tasks/t9": () =>
        ok({ id: "t9", status: "done", expert: { name: "代码审查专家" }, provider: { display_name: "X", model: "m" } }),
      "GET /api/admin/tasks/t9/messages": () =>
        ok([
          { event_sequence: 1, author: "user", content: "帮我看看这段代码" },
          { event_sequence: 2, author: "assistant", content: "<b>加粗诱导</b>内容" },
        ]),
      "GET /api/admin/tasks/t9/files": () => ok([]),
    });
    renderPage();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "查看上下文" }));
    await flush();
    // 第一步：任务 id 录入（举报条目不携带任务 id——message 举报 target 为消息 id）
    fireEvent.change(screen.getByLabelText("任务 ID"), { target: { value: "t9" } });
    fireEvent.click(screen.getByRole("button", { name: "打开任务上下文" }));
    await flush();

    // reason 前置：此刻尚未发出任何任务读取请求
    expect(screen.getByLabelText(REASON_LABEL)).toBeTruthy();
    const readCallsBefore = requestV2.mock.calls.filter(([path]) =>
      String(path).startsWith("/api/admin/tasks/t9")
    );
    expect(readCallsBefore.length).toBe(0);

    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "举报核查" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    });
    await flush();

    // 快照（元数据读免 reason）+ messages/files（内容读 reason 走查询串，D9）
    expect(requestV2).toHaveBeenCalledWith("/api/admin/tasks/t9", expect.anything());
    expect(requestV2).toHaveBeenCalledWith(
      expect.stringMatching(/^\/api\/admin\/tasks\/t9\/messages\?reason=/),
      expect.anything()
    );
    expect(requestV2).toHaveBeenCalledWith(
      expect.stringMatching(/^\/api\/admin\/tasks\/t9\/files\?direction=input&reason=/),
      expect.anything()
    );
    expect(requestV2).toHaveBeenCalledWith(
      expect.stringMatching(/^\/api\/admin\/tasks\/t9\/files\?direction=output&reason=/),
      expect.anything()
    );

    // 抽屉渲染：消息正文按字面文本渲染（含 <b> 不执行）
    const drawerText = screen.getByRole("dialog").textContent;
    expect(drawerText).toContain("帮我看看这段代码");
    expect(drawerText).toContain("<b>加粗诱导</b>内容");
    expect(document.querySelector("b")).toBeNull();
  });
});
