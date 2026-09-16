import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../../auth/AuthContext.jsx";
import { requestV2 } from "../../api/v2/client.js";
import RequireAdmin from "../../components/RequireAdmin.jsx";
import AuditPage from "./AuditPage.jsx";

// 审计查询页（Phase 8 T12b，测试清单⑥）：六维过滤组合进查询串（空维度跳过、
// 冒号经 encodeURIComponent）；detail JSON 树文本渲染；产物下载按钮（reason
// 弹窗→直链构造，直链 URL 携带 reason 查询参数——R7 知悉注记在弹窗描述中）。
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
        <AuditPage />
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

const REASON_LABEL = "操作原因（必填，审计留痕）";

const LOG_ARTIFACT = {
  id: "log-1",
  actor_id: "u-admin",
  action: "task.artifact.download",
  target_type: "task",
  target_id: "t1",
  reason: "运营排查",
  request_id: "req-1",
  detail: { task_id: "t1", file_id: "f1", sha256: "ab12" },
  created_at: "2026-09-15T08:00:00",
};

const LOG_PLAIN = {
  id: "log-2",
  actor_id: "u-admin",
  action: "user.suspend",
  target_type: "user",
  target_id: "u9",
  reason: "违规操作",
  request_id: null,
  detail: { user_id: "u9", before_status: "active", status: "suspended" },
  created_at: "2026-09-15T09:00:00",
};

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue(ADMIN_CTX);
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("⑥六维过滤组合请求构造", () => {
  it("全维度填写 → 查询串按序组合（空维度跳过、冒号编码）", async () => {
    stubByPath({ "GET /api/admin/audit-logs": () => ok({ items: [], total: 0, page: 1, size: 20 }) });
    renderPage();
    await flush();

    fireEvent.change(screen.getByLabelText("操作者 ID"), { target: { value: "u1" } });
    fireEvent.change(screen.getByLabelText("动作"), { target: { value: "user.suspend" } });
    fireEvent.change(screen.getByLabelText("目标类型"), { target: { value: "user" } });
    fireEvent.change(screen.getByLabelText("目标 ID"), { target: { value: "t1" } });
    fireEvent.change(screen.getByLabelText("起始时间"), { target: { value: "2026-09-16T08:00" } });
    fireEvent.change(screen.getByLabelText("截止时间"), { target: { value: "2026-09-17T08:00" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/audit-logs?actor_id=u1&action=user.suspend&target_type=user&target_id=t1" +
        "&since=2026-09-16T08%3A00&until=2026-09-17T08%3A00&page=1&size=20",
      expect.anything()
    );
  });

  it("仅填动作 → 其余维度不进查询串", async () => {
    stubByPath({ "GET /api/admin/audit-logs": () => ok({ items: [], total: 0, page: 1, size: 20 }) });
    renderPage();
    await flush();

    fireEvent.change(screen.getByLabelText("动作"), { target: { value: "user.suspend" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/audit-logs?action=user.suspend&page=1&size=20",
      expect.anything()
    );
  });
});

describe("detail JSON 树与产物下载", () => {
  it("详情展开：detail 以 JSON 文本树渲染（键值均为字面文本）", async () => {
    stubByPath({
      "GET /api/admin/audit-logs": () =>
        ok({ items: [LOG_ARTIFACT, LOG_PLAIN], total: 2, page: 1, size: 20 }),
    });
    renderPage();
    await flush();

    const row = screen.getByText("log-1").closest("tr");
    fireEvent.click(within(row).getByRole("button", { name: "详情" }));
    await flush();

    // JSON 树文本渲染：键与值均可见；detail 含 HTML 片段时不执行（防御性同规）
    const detailText = screen.getByLabelText("审计详情").textContent;
    expect(detailText).toContain("task_id");
    expect(detailText).toContain("t1");
    expect(detailText).toContain("file_id");
    expect(detailText).toContain("f1");
  });

  it("下载产物按钮仅在 detail 携带 task_id+file_id 的行出现", async () => {
    stubByPath({
      "GET /api/admin/audit-logs": () =>
        ok({ items: [LOG_ARTIFACT, LOG_PLAIN], total: 2, page: 1, size: 20 }),
    });
    renderPage();
    await flush();

    const artifactRow = screen.getByText("log-1").closest("tr");
    expect(within(artifactRow).getByRole("button", { name: "下载产物" })).toBeTruthy();
    const plainRow = screen.getByText("log-2").closest("tr");
    expect(within(plainRow).queryByRole("button", { name: "下载产物" })).toBeNull();
  });

  it("下载产物：reason 弹窗（R7 知悉注记）→ 直链构造含 reason 查询参数", async () => {
    stubByPath({
      "GET /api/admin/audit-logs": () => ok({ items: [LOG_ARTIFACT], total: 1, page: 1, size: 20 }),
    });
    renderPage();
    await flush();

    const clicked = [];
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function clickedAnchor() {
        // getAttribute 取原始 href（.href 会被 jsdom 解析为绝对 URL）
        clicked.push(this.getAttribute("href"));
      });

    fireEvent.click(screen.getByRole("button", { name: "下载产物" }));
    await flush();

    // R7 知悉注记：弹窗描述写明直链 URL 携带 reason 的留痕风险
    expect(screen.getByRole("dialog").textContent).toContain("URL");
    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "复检产物" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    });
    await flush();

    expect(clicked.length).toBe(1);
    // 原始 href 为相对直链（含 reason 查询参数）；以 URL 解析比对契约形状
    const resolved = new URL(clicked[0], "http://localhost/");
    expect(resolved.pathname + resolved.search).toBe(
      "/api/admin/tasks/t1/artifacts/f1/download?reason=%E5%A4%8D%E6%A3%80%E4%BA%A7%E7%89%A9"
    );
    clickSpy.mockRestore();
  });
});
