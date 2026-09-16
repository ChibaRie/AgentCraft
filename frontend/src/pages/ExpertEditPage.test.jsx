import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useParams, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import ExpertEditPage from "./ExpertEditPage.jsx";

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

const EXPERT_ID = "aaaaaaaa-1111-4222-8333-444444444444";
const REV1_ID = "bbbbbbbb-1111-4222-8333-444444444444";
const REV2_ID = "cccccccc-1111-4222-8333-444444444444";
const SKILL1_ID = "dddddddd-1111-4222-8333-444444444444";
const SKILL1_REV = "eeeeeeee-1111-4222-8333-444444444444";
const SKILL2_ID = "ffffffff-1111-4222-8333-444444444444";
const SKILL2_REV = "11111111-2222-4333-8444-555555555555";
const UNKNOWN_SKILL_ID = "99999999-1111-4222-8333-444444444444";
const UNKNOWN_SKILL_REV = "88888888-1111-4222-8333-444444444444";

/** expert content_json（§9.8.2 全字段；请求体即该形态、永不携带 hash 字段） */
const DRAFT_CONTENT = {
  name: "代码评审专家",
  description: "以严格标准审查代码质量与设计取舍",
  category: "tech",
  avatar_url: null,
  persona: "一位资深代码评审者，熟悉多语言工程实践",
  methodology: "先读结构，再读实现，最后看测试覆盖",
  task_examples: ["审查这段函数的边界条件"],
  skill_refs: [],
};

// GET /api/skills/public 卡五键（Phase 9 T2 §10.11(a)：不含方法论正文/owner）
const PUBLIC_SKILLS = [
  {
    id: SKILL1_ID,
    published_revision_id: SKILL1_REV,
    name: "周报整理",
    description: "把散落的每日记录汇总为周报。",
    category: "office",
  },
  {
    id: SKILL2_ID,
    published_revision_id: SKILL2_REV,
    name: "会议纪要",
    description: "从会议记录中提炼结论与待办。",
    category: "office",
  },
];

/** GET /api/experts/{id} 详情（author_service.get_entity 出参） */
function detail({ status = "draft", latestStatus = "draft", content = DRAFT_CONTENT } = {}) {
  return {
    expert: {
      id: EXPERT_ID,
      status,
      published_revision_id: status === "published" ? REV1_ID : null,
      created_at: "2026-09-01T08:00:00",
      updated_at: "2026-09-10T08:00:00",
    },
    revisions: [
      {
        revision_id: REV1_ID,
        revision_no: 1,
        status: latestStatus,
        content_sha256: "ab".repeat(32),
        content_json: content,
        created_at: "2026-09-01T08:00:00",
        updated_at: "2026-09-10T08:00:00",
      },
    ],
  };
}

/** create/PUT 响应 {entity, revision} */
function savedBody({ revisionId = REV1_ID, revisionNo = 1, revisionStatus = "draft", entityStatus = "draft" } = {}) {
  return {
    entity: {
      id: EXPERT_ID,
      status: entityStatus,
      published_revision_id: entityStatus === "published" ? REV1_ID : null,
      created_at: "2026-09-01T08:00:00",
      updated_at: "2026-09-14T08:00:00",
    },
    revision: {
      revision_id: revisionId,
      revision_no: revisionNo,
      status: revisionStatus,
      content_sha256: "ef".repeat(32),
      content_json: DRAFT_CONTENT,
      created_at: "2026-09-14T08:00:00",
      updated_at: "2026-09-14T08:00:00",
    },
  };
}

const V2_AUTHOR = { email: "author@example.com", entitlements: ["expert_author"] };

function EditRoute() {
  const { id } = useParams();
  return <ExpertEditPage expertId={id} />;
}

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/my-experts/new" element={<ExpertEditPage />} />
        <Route path="/my-experts/:id/edit" element={<EditRoute />} />
      </Routes>
    </MemoryRouter>
  );
}

/** 编辑模式挂载（并行 GET 详情 + 已发布 skills）并等待落定 */
async function renderEditLoaded(options = {}) {
  requestV2.mockResolvedValueOnce(ok(detail(options)));
  requestV2.mockResolvedValueOnce(ok(PUBLIC_SKILLS));
  renderAt(`/my-experts/${EXPERT_ID}/edit`);
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
  it("entitlements 缺 expert_author → 整页 403 引导面，不发任何请求", async () => {
    useAuth.mockReturnValue({ v2User: { email: "x@example.com", entitlements: [] } });
    renderAt(`/my-experts/${EXPERT_ID}/edit`);
    await flush();

    expect(requestV2).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("403");
    expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
  });
});

describe("新建（POST 全量 content_json）", () => {
  it("填写表单保存 → POST /api/experts 幂等键 + 全字段 body → 导航至编辑页并提示已创建", async () => {
    requestV2.mockResolvedValueOnce(ok([])); // 新建模式同样预取候选 Skill（挂载即发）
    renderAt("/my-experts/new");
    await flush();

    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "代码评审专家" } });
    fireEvent.change(screen.getByLabelText("简介"), {
      target: { value: "以严格标准审查代码质量与设计取舍" },
    });
    fireEvent.change(screen.getByLabelText("分类"), { target: { value: "tech" } });
    fireEvent.change(screen.getByLabelText("人设"), {
      target: { value: "一位资深代码评审者，熟悉多语言工程实践" },
    });
    fireEvent.change(screen.getByLabelText("方法论"), {
      target: { value: "先读结构，再读实现，最后看测试覆盖" },
    });

    requestV2.mockResolvedValueOnce(ok(savedBody()));
    requestV2.mockResolvedValueOnce(ok(detail())); // 导航后编辑页装载：详情
    requestV2.mockResolvedValueOnce(ok([])); // 导航后编辑页装载：候选 skills
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    const [path, options] = requestV2.mock.calls[1];
    expect(path).toBe("/api/experts");
    expect(options.method).toBe("POST");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toEqual({
      name: "代码评审专家",
      description: "以严格标准审查代码质量与设计取舍",
      category: "tech",
      avatar_url: null,
      persona: "一位资深代码评审者，熟悉多语言工程实践",
      methodology: "先读结构，再读实现，最后看测试覆盖",
      task_examples: [],
      skill_refs: [],
    });
    expect(screen.getByText(/已创建为草稿/)).toBeTruthy();
  });
});

describe("编辑装载", () => {
  it("并行 GET 详情与公开 skills 枚举，表单回填最新 revision 的 content_json", async () => {
    await renderEditLoaded();

    expect(requestV2.mock.calls[0][0]).toBe(`/api/experts/${EXPERT_ID}`);
    expect(requestV2.mock.calls[1][0]).toBe("/api/skills/public");
    expect(screen.getByLabelText("名称").value).toBe("代码评审专家");
    expect(screen.getByLabelText("方法论").value).toBe("先读结构，再读实现，最后看测试覆盖");
    expect(screen.getByLabelText("任务示例 1").value).toBe("审查这段函数的边界条件");
    expect(screen.getByText("草稿")).toBeTruthy();
    // 已发布候选进入多选
    expect(screen.getByLabelText("周报整理")).toBeTruthy();
    expect(screen.getByLabelText("会议纪要")).toBeTruthy();
  });
});

describe("保存（PUT 全量替换，简报②④）", () => {
  it("PUT body 为 content_json 全字段，且无 hash 字段", async () => {
    await renderEditLoaded();
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "代码评审专家V2" } });

    requestV2.mockResolvedValueOnce(ok(savedBody()));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    const [path, options] = requestV2.mock.calls[2];
    expect(path).toBe(`/api/experts/${EXPERT_ID}`);
    expect(options.method).toBe("PUT");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toEqual({
      name: "代码评审专家V2",
      description: "以严格标准审查代码质量与设计取舍",
      category: "tech",
      avatar_url: null,
      persona: "一位资深代码评审者，熟悉多语言工程实践",
      methodology: "先读结构，再读实现，最后看测试覆盖",
      task_examples: ["审查这段函数的边界条件"],
      skill_refs: [],
    });
    expect("content_sha256" in options.body).toBe(false);
    expect("hash" in options.body).toBe(false);
  });

  it("最新 revision 为 draft → 覆写文案", async () => {
    await renderEditLoaded();
    requestV2.mockResolvedValueOnce(ok(savedBody()));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    expect(screen.getByText("已保存：草稿内容已更新。")).toBeTruthy();
  });

  it("最新 revision 非 draft（已发布后编辑）→ 自动新 draft 双文案（含版本号）", async () => {
    await renderEditLoaded({ status: "published", latestStatus: "approved" });
    requestV2.mockResolvedValueOnce(
      ok(savedBody({ revisionId: REV2_ID, revisionNo: 2, entityStatus: "published" }))
    );
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    const notice = screen.getByText(/已保存为新草稿版本/);
    expect(notice.textContent).toContain("第 2 版");
  });
});

describe("提交审核（简报③ + harness 排除）", () => {
  it("draft revision 提审 → POST submit {tools:[]} 幂等键 → 徽标变审核中", async () => {
    await renderEditLoaded();
    requestV2.mockResolvedValueOnce(
      ok({ revision: { revision_id: REV1_ID, revision_no: 1, status: "pending_review" } })
    );
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));
    await flush();

    const [path, options] = requestV2.mock.calls[2];
    expect(path).toBe(`/api/experts/${EXPERT_ID}/revisions/${REV1_ID}/submit`);
    expect(options.method).toBe("POST");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toEqual({ tools: [] });
    expect(screen.getByText("审核中")).toBeTruthy();
    expect(screen.getByText(/已提交审核/)).toBeTruthy();
  });

  it("409 REVIEW_PENDING → 服务端文案进错误面", async () => {
    await renderEditLoaded();
    requestV2.mockRejectedValueOnce(
      v2Error("REVIEW_PENDING", "仅 draft 状态可提交审核", 409)
    );
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("仅 draft 状态可提交审核");
    expect(screen.getByText("草稿")).toBeTruthy();
  });

  it("工具多选静态排除 harness-kind 项（check_code_style 不出现）；勾选工具进 submit body", async () => {
    await renderEditLoaded();

    expect(screen.queryByLabelText(/check_code_style/)).toBeNull();
    fireEvent.click(screen.getByLabelText("读取任务输入文件（read_task_file）"));

    requestV2.mockResolvedValueOnce(
      ok({ revision: { revision_id: REV1_ID, revision_no: 1, status: "pending_review" } })
    );
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));
    await flush();

    const [, options] = requestV2.mock.calls[2];
    expect(options.body).toEqual({
      tools: [{ tool_id: "read_task_file", version: "1" }],
    });
  });
});

describe("skill_refs 组装（多选）", () => {
  it("勾选已发布 skills 钉 published_revision_id；既有引用不在候选集时保留（round-trip）", async () => {
    const withRef = {
      ...DRAFT_CONTENT,
      skill_refs: [{ skill_id: UNKNOWN_SKILL_ID, revision_id: UNKNOWN_SKILL_REV }],
    };
    await renderEditLoaded({ content: withRef });

    // 既有引用（如他人 published skill）以禁用勾选项保留，防保存时静默丢失
    expect(screen.getByLabelText(UNKNOWN_SKILL_ID).checked).toBe(true);
    expect(screen.getByLabelText(UNKNOWN_SKILL_ID).disabled).toBe(true);

    fireEvent.click(screen.getByLabelText("周报整理"));
    fireEvent.click(screen.getByLabelText("会议纪要"));

    requestV2.mockResolvedValueOnce(ok(savedBody()));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    const [, options] = requestV2.mock.calls[2];
    // 契约注记：ref 钉实体当前 published revision（approve 断言 skill_revision
    // published；latest 可能是发布后新开 draft，不能取 latest_revision）
    expect(options.body.skill_refs).toEqual([
      { skill_id: UNKNOWN_SKILL_ID, revision_id: UNKNOWN_SKILL_REV },
      { skill_id: SKILL1_ID, revision_id: SKILL1_REV },
      { skill_id: SKILL2_ID, revision_id: SKILL2_REV },
    ]);
  });
});
