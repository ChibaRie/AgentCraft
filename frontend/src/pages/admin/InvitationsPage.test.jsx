import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../../auth/AuthContext.jsx";
import { requestV2 } from "../../api/v2/client.js";
import RequireAdmin from "../../components/RequireAdmin.jsx";
import InvitationsPage from "./InvitationsPage.jsx";

// 邀请管理页（Phase 8 T12a，测试清单④）：创建载荷 expires_in_days + 幂等键、
// 四态过滤、revoke 二次确认、「邮件已发出」提示且绝不渲染 token（Sup:132）。
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
        <InvitationsPage />
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

/** 按「METHOD path」分派的可编程打桩（页面多请求编排，避免 Once 队列脆弱） */
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

const INVITATION_ROW = {
  id: "i1",
  email: "newbie@example.com",
  status: "open",
  created_at: "2026-09-10T08:00:00",
  expires_at: "2026-09-17T08:00:00",
  consumed_at: null,
  revoked_at: null,
};
const LIST_PAGE = { items: [INVITATION_ROW], total: 1, page: 1, size: 20 };
// 契约响应不含 token（Sup:132）——多塞一枚 token 断言 UI 绝不渲染
const CREATED = {
  id: "i2",
  email: "creator@example.com",
  expires_at: "2026-09-23T08:00:00",
  token: "SUPER-SECRET-TOKEN",
};

const REASON_LABEL = "操作原因（必填，审计留痕）";

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue(ADMIN_CTX);
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("列表装载与四态过滤", () => {
  it("挂载拉取列表并渲染行；状态过滤选择后带 status 参数重取", async () => {
    stubByPath({ "GET /api/admin/invitations": () => ok(LIST_PAGE) });
    renderPage();
    await flush();

    expect(requestV2).toHaveBeenCalledWith("/api/admin/invitations", expect.anything());
    expect(screen.getByText("newbie@example.com")).toBeTruthy();
    // 行内状态徽标（与过滤下拉的「待使用」选项区分，按行断言）
    const row = screen.getByText("newbie@example.com").closest("tr");
    expect(row.textContent).toContain("待使用");

    fireEvent.change(screen.getByLabelText("状态过滤"), { target: { value: "consumed" } });
    await flush();
    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/invitations?status=consumed",
      expect.anything()
    );
  });
});

describe("创建邀请（④）", () => {
  async function submitCreate() {
    renderPage();
    await flush();

    fireEvent.change(screen.getByLabelText("电子邮箱"), {
      target: { value: "creator@example.com" },
    });
    fireEvent.change(screen.getByLabelText("有效期（天）"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "创建邀请" }));
    await flush();
    fireEvent.change(screen.getByLabelText(REASON_LABEL), {
      target: { value: "新成员入职" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认创建" }));
    });
    await flush();
  }

  it("POST /invitations 载荷含 expires_in_days 与幂等键；成功提示邮件已发出", async () => {
    stubByPath({
      "GET /api/admin/invitations": () => ok(LIST_PAGE),
      "POST /api/admin/invitations": () => ok(CREATED),
    });
    await submitCreate();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/invitations",
      expect.objectContaining({
        method: "POST",
        body: { email: "creator@example.com", expires_in_days: 3, reason: "新成员入职" },
        idempotencyKey: expect.any(String),
      })
    );
    const notice = screen.getByRole("status");
    expect(notice.textContent).toContain("激活邮件已发出");
    expect(notice.textContent).toContain("creator@example.com");
  });

  it("绝不渲染 token（明文链接仅走邮件，Sup:132 红线）", async () => {
    stubByPath({
      "GET /api/admin/invitations": () => ok(LIST_PAGE),
      "POST /api/admin/invitations": () => ok(CREATED),
    });
    await submitCreate();

    expect(screen.queryByText(/SUPER-SECRET-TOKEN/)).toBeNull();
  });
});

describe("撤销邀请", () => {
  it("revoke 二次确认：POST /invitations/{id}/revoke 载荷仅 reason + 幂等键", async () => {
    stubByPath({
      "GET /api/admin/invitations": () => ok(LIST_PAGE),
      "POST /api/admin/invitations/i1/revoke": () =>
        ok({ id: "i1", email: "newbie@example.com", status: "revoked", revoked_at: "2026-09-11T08:00:00" }),
    });
    renderPage();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    await flush();
    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "误建撤销" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认撤销" }));
    });
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/invitations/i1/revoke",
      expect.objectContaining({
        method: "POST",
        body: { reason: "误建撤销" },
        idempotencyKey: expect.any(String),
      })
    );
  });

  it("已消费/已撤销行不提供撤销入口", async () => {
    stubByPath({
      "GET /api/admin/invitations": () =>
        ok({
          items: [{ ...INVITATION_ROW, id: "i3", status: "consumed" }],
          total: 1,
          page: 1,
          size: 20,
        }),
    });
    renderPage();
    await flush();

    expect(screen.queryByRole("button", { name: "撤销" })).toBeNull();
  });
});

describe("装载失败", () => {
  it("非 403 失败 → 页面内联告警（gate 不接管）", async () => {
    requestV2.mockImplementation(() =>
      Promise.reject(new Error("网络错误"))
    );
    renderPage();
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  });
});
