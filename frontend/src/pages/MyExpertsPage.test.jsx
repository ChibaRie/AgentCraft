import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import MyExpertsPage from "./MyExpertsPage.jsx";

// 页面级行为测试（Phase 8 T10）：mock useAuth（v2User entitlements gating）+ requestV2
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

/** 真实 V2ApiError 实例（组件按 code/status/message 分流） */
function v2Error(code, message, status) {
  return new V2ApiError(code, message, status);
}

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定） */
function flush() {
  return act(async () => {});
}

const EXPERT_ID = "aaaaaaaa-1111-4222-8333-444444444444";
const REV_ID = "bbbbbbbb-1111-4222-8333-444444444444";

/** 后端 GET /api/v2/experts 行形状（author_service.list_entities 出参） */
function row(overrides = {}) {
  return {
    id: EXPERT_ID,
    status: "draft",
    published_revision_id: null,
    revision_count: 1,
    latest_revision: {
      revision_id: REV_ID,
      revision_no: 1,
      status: "draft",
      content_sha256: "ab".repeat(32),
      updated_at: "2026-09-10T08:00:00",
    },
    name: "代码评审专家",
    ...overrides,
  };
}

const V2_AUTHOR = { email: "author@example.com", entitlements: ["expert_author"] };

function renderPage() {
  return render(
    <MemoryRouter>
      <MyExpertsPage />
    </MemoryRouter>
  );
}

/** 编排挂载（单 GET 列表）并等待加载落定 */
async function renderWithList(rows) {
  requestV2.mockResolvedValueOnce(ok(rows));
  renderPage();
  await flush();
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({ v2User: V2_AUTHOR });
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("expert_author 门（简报⑦）", () => {
  it.each([
    ["无 v2User", null],
    ["entitlements 缺 expert_author", { email: "x@example.com", entitlements: [] }],
  ])("%s → 整页 403 引导面，不发任何请求", async (_name, v2User) => {
    useAuth.mockReturnValue({ v2User });
    renderPage();
    await flush();

    expect(requestV2).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("403");
    expect(screen.getByRole("alert").textContent).toContain("expert_author");
    expect(screen.queryByRole("link", { name: "新建专家" })).toBeNull();
  });
});

describe("列表装载与状态徽标（简报①）", () => {
  it("挂载 GET /api/v2/experts，四态徽标渲染：草稿/已发布/审核中/已驳回", async () => {
    await renderWithList([
      row({ id: "aaaaaaaa-0000-4222-8333-444444444441", name: "甲", status: "draft" }),
      row({
        id: "aaaaaaaa-0000-4222-8333-444444444442",
        name: "乙",
        status: "published",
        published_revision_id: REV_ID,
        latest_revision: {
          revision_id: REV_ID,
          revision_no: 1,
          status: "approved",
          content_sha256: "ab".repeat(32),
          updated_at: "2026-09-10T08:00:00",
        },
      }),
      row({
        id: "aaaaaaaa-0000-4222-8333-444444444443",
        name: "丙",
        status: "draft",
        latest_revision: {
          revision_id: REV_ID,
          revision_no: 1,
          status: "pending_review",
          content_sha256: "ab".repeat(32),
          updated_at: "2026-09-10T08:00:00",
        },
      }),
      row({
        id: "aaaaaaaa-0000-4222-8333-444444444444",
        name: "丁",
        status: "draft",
        latest_revision: {
          revision_id: REV_ID,
          revision_no: 1,
          status: "rejected",
          content_sha256: "ab".repeat(32),
          updated_at: "2026-09-10T08:00:00",
        },
      }),
    ]);

    expect(requestV2.mock.calls[0][0]).toBe("/api/v2/experts");
    expect(requestV2.mock.calls[0][1]?.method).toBeUndefined();
    expect(screen.getByText("甲")).toBeTruthy();
    expect(screen.getByText("乙")).toBeTruthy();
    expect(screen.getByText("丙")).toBeTruthy();
    expect(screen.getByText("丁")).toBeTruthy();
    expect(screen.getByText("草稿")).toBeTruthy();
    expect(screen.getByText("已发布")).toBeTruthy();
    expect(screen.getByText("审核中")).toBeTruthy();
    expect(screen.getByText("已驳回")).toBeTruthy();
    expect(screen.getByText("共 4 位专家")).toBeTruthy();
  });

  it("GET 失败 → 内联错误文案，不渲染行", async () => {
    requestV2.mockRejectedValueOnce(v2Error("INTERNAL", "服务暂不可用", 500));
    renderPage();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("服务暂不可用");
    expect(screen.queryByText("代码评审专家")).toBeNull();
  });

  it("空列表 → 空态引导", async () => {
    await renderWithList([]);

    expect(screen.getByText("还没有专家")).toBeTruthy();
    expect(screen.getByText("共 0 位专家")).toBeTruthy();
  });
});

describe("下架（T6 offline，简报⑤）", () => {
  it("published 行点击下架 → POST offline 幂等键无 body → 徽标即时变草稿（无 refetch）", async () => {
    await renderWithList([
      row({
        status: "published",
        published_revision_id: REV_ID,
        latest_revision: {
          revision_id: REV_ID,
          revision_no: 1,
          status: "approved",
          content_sha256: "ab".repeat(32),
          updated_at: "2026-09-10T08:00:00",
        },
      }),
    ]);
    expect(screen.getByText("已发布")).toBeTruthy();

    requestV2.mockResolvedValueOnce(
      ok({ entity: { id: EXPERT_ID, status: "draft", published_revision_id: REV_ID } })
    );
    fireEvent.click(screen.getByRole("button", { name: "下架" }));
    await flush();

    const [path, options] = requestV2.mock.calls[1];
    expect(path).toBe(`/api/v2/experts/${EXPERT_ID}/offline`);
    expect(options.method).toBe("POST");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toBeUndefined();
    // 徽标即时变化（本地 patch，非 refetch）
    expect(screen.getByText("草稿")).toBeTruthy();
    expect(screen.queryByText("已发布")).toBeNull();
    expect(requestV2).toHaveBeenCalledTimes(2);
  });
});

describe("删除（T6 DELETE，简报⑥）", () => {
  async function openConfirm() {
    await renderWithList([row()]);
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await flush();
  }

  it("确认删除 → DELETE 幂等键 → 行移出列表", async () => {
    await openConfirm();
    expect(screen.getByText(/确认删除「代码评审专家」/)).toBeTruthy();

    requestV2.mockResolvedValueOnce(
      ok({ deleted: true, entity: { id: EXPERT_ID, status: "draft" } })
    );
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await flush();

    const [path, options] = requestV2.mock.calls[1];
    expect(path).toBe(`/api/v2/experts/${EXPERT_ID}`);
    expect(options.method).toBe("DELETE");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(screen.queryByText("代码评审专家")).toBeNull();
    expect(screen.getByText("还没有专家")).toBeTruthy();
  });

  it("409 ENTITY_IN_USE → 服务端引用计数文案进错误面，行保留", async () => {
    await openConfirm();
    requestV2.mockRejectedValueOnce(v2Error("ENTITY_IN_USE", "被 2 个任务引用", 409));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("被 2 个任务引用");
    expect(screen.getByText("代码评审专家")).toBeTruthy();
  });

  it("取消关闭确认条，不发请求", async () => {
    await openConfirm();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await flush();

    expect(screen.queryByText(/确认删除/)).toBeNull();
    expect(requestV2).toHaveBeenCalledTimes(1);
  });
});
