import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../../auth/AuthContext.jsx";
import { requestV2 } from "../../api/v2/client.js";
import RequireAdmin from "../../components/RequireAdmin.jsx";
import ReviewsPage from "./ReviewsPage.jsx";

// 审核队列页（Phase 8 T12b，测试清单①）：content_json text-only 渲染（行为化
// 逃逸测试：`<img onerror>`/`<script>` 渲染后 DOM 为字面文本零元素注入——安全
// Minor 1/R6）+ auto_check 徽标 + approve/reject（target_type 随 body）+
// approve 成功展示 published_revision_id（Minor 7：以 Phase 7 reviews.py 实际
// 返回形状 {entity_id, entity_status, published_revision_id,
// previous_published_revision_id, revision_no, content_sha256} 为准）。
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
        <ReviewsPage />
      </RequireAdmin>
    </MemoryRouter>
  );
}

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

function flush() {
  return act_wrapper();
}

async function act_wrapper() {
  const { act } = await import("@testing-library/react");
  await act(async () => {});
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

const REVISION_A = {
  id: "r1",
  target_type: "expert_revision",
  revision_no: 3,
  owner_id: "u-author",
  content_json: {
    name: "代码审查专家",
    description: "资深代码审查",
    system_prompt: "你是资深代码审查专家",
  },
  content_sha256: "a".repeat(64),
  auto_check: { valid: true, issues: [] },
  created_at: "2026-09-14T08:00:00",
};

const REVISION_B = {
  id: "r2",
  target_type: "skill_revision",
  revision_no: 1,
  owner_id: "u-author-2",
  content_json: {
    name: "越狱技能",
    description: "命中越狱模板且带可执行代码片段",
  },
  content_sha256: "b".repeat(64),
  auto_check: {
    valid: false,
    issues: [
      { field: "description", rule: "jailbreak_template", level: "WARNING", message: "description 命中越狱模板，标记不通过" },
    ],
  },
  created_at: "2026-09-14T09:00:00",
};

const QUEUE = { items: [REVISION_A, REVISION_B], total: 2, page: 1, size: 20 };

const REASON_LABEL = "操作原因（必填，审计留痕）";

/** 打开 r1 的内容抽屉 */
async function openDrawer(routes, revisionId) {
  stubByPath(routes);
  renderPage();
  await flush();
  const row = screen.getByText(revisionId).closest("tr");
  fireEvent.click(within(row).getByRole("button", { name: "查看内容" }));
  await flush();
  return within(screen.getByRole("dialog"));
}

async function confirmPrompt() {
  fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "运营处置" } });
  const { act } = await import("@testing-library/react");
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
  });
  await act_wrapper();
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue(ADMIN_CTX);
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("队列与过滤", () => {
  it("挂载拉取审核队列；target_type 过滤进查询串", async () => {
    stubByPath({ "GET /api/admin/reviews": () => ok(QUEUE) });
    renderPage();
    await flush();

    expect(requestV2).toHaveBeenCalledWith("/api/admin/reviews", expect.anything());
    expect(screen.getByText("r1")).toBeTruthy();
    expect(screen.getByText("r2")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("目标类型"), {
      target: { value: "skill_revision" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/reviews?target_type=skill_revision",
      expect.anything()
    );
  });

  it("auto_check 徽标：通过/未通过（N 项）+ issue 明细渲染", async () => {
    stubByPath({ "GET /api/admin/reviews": () => ok(QUEUE) });
    renderPage();
    await flush();

    const rowA = screen.getByText("r1").closest("tr");
    expect(rowA.textContent).toContain("自动检查通过");
    const rowB = screen.getByText("r2").closest("tr");
    expect(rowB.textContent).toContain("自动检查未通过");
    expect(rowB.textContent).toContain("1 项");
  });
});

describe("①content_json text-only 渲染（安全 Minor 1 / R6）", () => {
  it("content_json 含 <img onerror>/<script> 时零元素注入，DOM 为字面文本", async () => {
    const poisoned = {
      name: "投毒专家",
      description: '<img src=x onerror="window.__pwned=1">',
      system_prompt: "<script>window.__pwned=1</script>",
    };
    await openDrawer(
      {
        "GET /api/admin/reviews": () =>
          ok({ items: [{ ...REVISION_A, content_json: poisoned }], total: 1, page: 1, size: 20 }),
      },
      "r1"
    );

    // 零元素注入：img/script 均未成为 DOM 元素，载荷未执行
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
    expect(window.__pwned).toBeUndefined();

    // 字面文本可见（审核员看到的就是原文；JSON 序列化仅转义引号不改写标签语义）
    expect(screen.getByRole("dialog").textContent).toContain("<img src=x onerror=");
    expect(screen.getByRole("dialog").textContent).toContain("<script>window.__pwned=1</script>");
  });

  it("静态断言：四个治理页源码零 dangerouslySetInnerHTML 使用", async () => {
    const sources = import.meta.glob(
      ["./ReviewsPage.jsx", "./ReportsPage.jsx", "./CatalogPage.jsx", "./AuditPage.jsx"],
      { query: "?raw", import: "default", eager: true }
    );
    expect(Object.keys(sources).length).toBe(4);
    for (const [file, source] of Object.entries(sources)) {
      expect(String(source), `${file} 不得使用 dangerouslySetInnerHTML`).not.toContain(
        "dangerouslySetInnerHTML"
      );
    }
  });
});

describe("approve/reject（target_type 随 body；Minor 7 approve 形状）", () => {
  it("approve：POST body {target_type, reason}；回执展示 published_revision_id（Phase 7 实际形状）", async () => {
    const drawer = await openDrawer(
      {
        "GET /api/admin/reviews": () => ok(QUEUE),
        "POST /api/admin/reviews/r1/approve": () =>
          ok({
            entity_id: "e1",
            entity_status: "published",
            published_revision_id: "r1",
            previous_published_revision_id: "r0",
            revision_no: 3,
            content_sha256: "a".repeat(64),
          }),
      },
      "r1"
    );

    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "通过发布" }));
    await flush();
    await confirmPrompt();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/reviews/r1/approve",
      expect.objectContaining({
        method: "POST",
        body: { target_type: "expert_revision", reason: "运营处置" },
        idempotencyKey: expect.any(String),
      })
    );
    const dialogText = screen.getByRole("dialog").textContent;
    expect(dialogText).toContain("published_revision_id");
    expect(dialogText).toContain("r1");
    expect(dialogText).toContain("r0"); // previous_published_revision_id 一并可见
  });

  it("reject：POST body {target_type, reason}；回执确认驳回（实体不发布）", async () => {
    const drawer = await openDrawer(
      {
        "GET /api/admin/reviews": () => ok(QUEUE),
        "POST /api/admin/reviews/r2/reject": () =>
          ok({ revision_id: "r2", status: "rejected" }),
      },
      "r2"
    );

    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "驳回" }));
    await flush();
    await confirmPrompt();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/reviews/r2/reject",
      expect.objectContaining({
        method: "POST",
        body: { target_type: "skill_revision", reason: "运营处置" },
        idempotencyKey: expect.any(String),
      })
    );
    expect(screen.getByRole("dialog").textContent).toContain("rejected");
  });

  it("latest-call 守卫：旧查询后返回时被丢弃，队列保持最新一次结果（Phase 9 T7）", async () => {
    const REVISION_B = {
      ...REVISION_A,
      id: "r9",
      revision_no: 9,
    };
    let releaseFirst;
    const firstPending = new Promise((resolve) => {
      releaseFirst = () => resolve(ok({ items: [REVISION_A], total: 1, page: 1, size: 20 }));
    });
    let call = 0;
    requestV2.mockImplementation(() => {
      call += 1;
      if (call === 1) {
        return firstPending;
      }
      return Promise.resolve(ok({ items: [REVISION_B], total: 1, page: 1, size: 20 }));
    });

    renderPage();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await flush();
    expect(screen.getByText("r9")).toBeTruthy();
    expect(screen.getByText("第 9 版")).toBeTruthy();

    // 旧查询随后返回：守卫应丢弃，不回滚为旧结果
    const { act } = await import("@testing-library/react");
    await act(async () => {
      releaseFirst();
      await Promise.resolve();
    });
    await flush();
    expect(screen.getByText("r9")).toBeTruthy();
    expect(screen.queryByText("r1")).toBeNull();
  });
});
