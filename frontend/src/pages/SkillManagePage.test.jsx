import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import SkillManagePage from "./SkillManagePage.jsx";

// 页面级行为测试（Phase 8 T10）：mock useAuth（v2User entitlements gating）+ requestV2
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

function v2Error(code, message, status) {
  return new V2ApiError(code, message, status);
}

function flush() {
  return act(async () => {});
}

const SKILL_ID = "dddddddd-1111-4222-8333-444444444444";
const REV_ID = "eeeeeeee-1111-4222-8333-444444444444";

/** 后端 GET /api/v2/skills 行形状（author_service.list_entities 出参） */
function row(overrides = {}) {
  return {
    id: SKILL_ID,
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
    name: "周报整理",
    ...overrides,
  };
}

const V2_AUTHOR = { email: "author@example.com", entitlements: ["expert_author"] };

function renderPage() {
  return render(
    <MemoryRouter>
      <SkillManagePage />
    </MemoryRouter>
  );
}

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
    ["entitlements 缺 expert_author", { email: "x@example.com", entitlements: ["other"] }],
  ])("%s → 整页 403 引导面，不发任何请求", async (_name, v2User) => {
    useAuth.mockReturnValue({ v2User });
    renderPage();
    await flush();

    expect(requestV2).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("403");
    expect(screen.getByRole("alert").textContent).toContain("expert_author");
    expect(screen.queryByRole("button", { name: "新建 Skill" })).toBeNull();
  });
});

describe("列表装载与 V2 范式钉死", () => {
  it("挂载 GET /api/v2/skills，行渲染 + 徽标（草稿/已发布）", async () => {
    await renderWithList([
      row(),
      row({
        id: "dddddddd-9999-4222-8333-444444444444",
        name: "会议纪要",
        status: "published",
        published_revision_id: REV_ID,
        latest_revision: {
          revision_id: REV_ID,
          revision_no: 1,
          status: "approved",
          content_sha256: "ab".repeat(32),
          updated_at: "2026-09-11T08:00:00",
        },
      }),
    ]);

    expect(requestV2.mock.calls[0][0]).toBe("/api/v2/skills");
    expect(requestV2.mock.calls[0][1]?.method).toBeUndefined();
    expect(screen.getByText("周报整理")).toBeTruthy();
    expect(screen.getByText("会议纪要")).toBeTruthy();
    expect(screen.getByText("草稿")).toBeTruthy();
    expect(screen.getByText("已发布")).toBeTruthy();
    expect(screen.getByText("共 2 个 Skill")).toBeTruthy();
  });

  it("zip 导入入口移除；发布/校验/绑定情况等 V1 直发按钮不存在（D3a 下线声明）", async () => {
    await renderWithList([row()]);

    expect(screen.queryByRole("button", { name: "导入 Skill" })).toBeNull();
    expect(screen.queryByRole("button", { name: "发布" })).toBeNull();
    expect(screen.queryByRole("button", { name: "校验" })).toBeNull();
    expect(screen.queryByRole("button", { name: "绑定情况" })).toBeNull();
    expect(screen.queryByLabelText("选择要导入的 Skill 文件")).toBeNull();
  });

  it("GET 失败 → 内联错误文案", async () => {
    requestV2.mockRejectedValueOnce(v2Error("INTERNAL", "服务暂不可用", 500));
    renderPage();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("服务暂不可用");
  });
});

describe("提交审核（draft latest）", () => {
  it("POST submit {tools:[]} 幂等键 → 徽标即时变审核中", async () => {
    await renderWithList([row()]);

    requestV2.mockResolvedValueOnce(
      ok({ revision: { revision_id: REV_ID, revision_no: 1, status: "pending_review" } })
    );
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));
    await flush();

    const [path, options] = requestV2.mock.calls[1];
    expect(path).toBe(`/api/v2/skills/${SKILL_ID}/revisions/${REV_ID}/submit`);
    expect(options.method).toBe("POST");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toEqual({ tools: [] });
    expect(screen.getByText("审核中")).toBeTruthy();
    expect(screen.queryByText("草稿")).toBeNull();
  });
});

describe("下架（T6 offline）", () => {
  it("published 行点击下架 → POST offline 幂等键 → 徽标即时变草稿", async () => {
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
      ok({ entity: { id: SKILL_ID, status: "draft", published_revision_id: REV_ID } })
    );
    fireEvent.click(screen.getByRole("button", { name: "下架" }));
    await flush();

    const [path, options] = requestV2.mock.calls[1];
    expect(path).toBe(`/api/v2/skills/${SKILL_ID}/offline`);
    expect(options.method).toBe("POST");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(screen.getByText("草稿")).toBeTruthy();
    expect(screen.queryByText("已发布")).toBeNull();
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

    requestV2.mockResolvedValueOnce(
      ok({ deleted: true, entity: { id: SKILL_ID, status: "draft" } })
    );
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await flush();

    const [path, options] = requestV2.mock.calls[1];
    expect(path).toBe(`/api/v2/skills/${SKILL_ID}`);
    expect(options.method).toBe("DELETE");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(screen.queryByText("周报整理")).toBeNull();
    expect(screen.getByText("还没有 Skill")).toBeTruthy();
  });

  it("409 ENTITY_IN_USE → 服务端引用计数文案进错误面，行保留", async () => {
    await openConfirm();
    requestV2.mockRejectedValueOnce(v2Error("ENTITY_IN_USE", "被 3 个专家引用", 409));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("被 3 个专家引用");
    expect(screen.getByText("周报整理")).toBeTruthy();
  });
});

describe("编辑器入口", () => {
  it("新建 Skill 打开编辑弹窗；编辑行打开弹窗（detail 装载后带名标题）", async () => {
    await renderWithList([row()]);

    fireEvent.click(screen.getByRole("button", { name: "新建 Skill" }));
    await flush();
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(within(screen.getByRole("dialog")).getByText("新建 Skill")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await flush();
    expect(screen.queryByRole("dialog")).toBeNull();

    // 编辑模式：弹窗自取详情（GET /api/v2/skills/{id}）后以最新 revision 回填
    requestV2.mockResolvedValueOnce(
      ok({
        skill: { id: SKILL_ID, status: "draft", published_revision_id: null },
        revisions: [
          {
            revision_id: REV_ID,
            revision_no: 1,
            status: "draft",
            content_sha256: "ab".repeat(32),
            content_json: {
              name: "周报整理",
              description: "把本周散乱记录整理成结构化周报",
              use_case: "每周五需要把项目群里的进展整理成一份可发送的周报",
              role: "一名严谨的项目助理",
              goal: "产出一份结构完整、可直接发送的周报",
              steps: "先收集群消息，再按项目归类，最后提炼风险与下周计划",
              input_requirements: null,
              output_requirements: "Markdown 周报，含进展/风险/计划三节",
              constraints: "不得虚构未提及的进展",
            },
            created_at: "2026-09-01T08:00:00",
            updated_at: "2026-09-10T08:00:00",
          },
        ],
      })
    );
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    await flush();
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("编辑「周报整理」")).toBeTruthy();
    expect(screen.getByLabelText("名称").value).toBe("周报整理");
  });
});
