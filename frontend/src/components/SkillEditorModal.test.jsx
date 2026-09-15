import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import SkillEditorModal, { validateSkillField } from "./SkillEditorModal.jsx";

// 组件级行为测试（Phase 8 T10）：mock useAuth + requestV2
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

function flush() {
  return act(async () => {});
}

const SKILL_ID = "dddddddd-1111-4222-8333-444444444444";
const REV_ID = "eeeeeeee-1111-4222-8333-444444444444";

const CONTENT = {
  name: "周报整理",
  description: "把本周散乱记录整理成结构化周报",
  use_case: "每周五需要把项目群里的进展整理成一份可发送的周报",
  role: "一名严谨的项目助理",
  goal: "产出一份结构完整、重点突出、可直接发送给项目干系人的周报",
  steps: "先收集群消息，再按项目归类，最后提炼风险与下周计划",
  input_requirements: null,
  output_requirements: "Markdown 周报，含进展/风险/计划三节",
  constraints: "只基于群内消息整理，不得虚构任何未提及的进展或风险",
};

/** GET /api/v2/skills/{id} 详情（author_service.get_entity 出参） */
function detail() {
  return {
    skill: { id: SKILL_ID, status: "draft", published_revision_id: null },
    revisions: [
      {
        revision_id: REV_ID,
        revision_no: 1,
        status: "draft",
        content_sha256: "ab".repeat(32),
        content_json: CONTENT,
        created_at: "2026-09-01T08:00:00",
        updated_at: "2026-09-10T08:00:00",
      },
    ],
  };
}

const V2_AUTHOR = { email: "author@example.com", entitlements: ["expert_author"] };

function renderModal(skillId = null) {
  return render(
    <SkillEditorModal skillId={skillId} onClose={vi.fn()} onSaved={vi.fn()} />
  );
}

async function renderEditLoaded() {
  requestV2.mockResolvedValueOnce(ok(detail()));
  renderModal(SKILL_ID);
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

describe("字段边界（§9.8.2）", () => {
  it("validateSkillField 边界钉死：下界/上界/可选", () => {
    expect(validateSkillField("name", "长")).toBe("名称需为 2-30 个字符");
    expect(validateSkillField("name", "长".repeat(31))).toBe("名称需为 2-30 个字符");
    expect(validateSkillField("name", "周报整理")).toBe("");
    expect(validateSkillField("use_case", "太短的场景")).toBe("使用场景需为 20-5000 个字符");
    expect(validateSkillField("role", "角色")).toBe("AI 角色需为 5-200 个字符");
    expect(validateSkillField("description", "八个字描述")).toBe("描述需为 10-200 个字符");
    // input_requirements 可选：空与 ≤5000 均通过
    expect(validateSkillField("input_requirements", "")).toBe("");
    expect(validateSkillField("input_requirements", "x".repeat(5000))).toBe("");
    expect(validateSkillField("input_requirements", "x".repeat(5001))).toBe(
      "输入要求不能超过 5000 字符"
    );
  });

  it("新建弹窗渲染九字段表单", () => {
    renderModal();

    expect(screen.getByRole("dialog", { name: "新建 Skill" })).toBeTruthy();
    for (const label of [
      "名称",
      "描述",
      "AI 角色",
      "使用场景",
      "任务目标",
      "工作步骤",
      "输入要求（可选）",
      "输出要求",
      "约束",
    ]) {
      expect(screen.getByLabelText(label)).toBeTruthy();
    }
  });
});

describe("创建（POST 全量 content_json）", () => {
  it("POST /api/v2/skills 幂等键 + 九字段 body（input_requirements 空→null，无 hash 字段）→ onSaved", async () => {
    renderModal();
    await flush();

    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "周报整理" } });
    fireEvent.change(screen.getByLabelText("描述"), {
      target: { value: "把本周散乱记录整理成结构化周报" },
    });
    fireEvent.change(screen.getByLabelText("AI 角色"), { target: { value: "一名严谨的项目助理" } });
    fireEvent.change(screen.getByLabelText("使用场景"), {
      target: { value: "每周五需要把项目群里的进展整理成一份可发送的周报" },
    });
    fireEvent.change(screen.getByLabelText("任务目标"), {
      target: { value: "产出一份结构完整、重点突出、可直接发送给项目干系人的周报" },
    });
    fireEvent.change(screen.getByLabelText("工作步骤"), {
      target: { value: "先收集群消息，再按项目归类，最后提炼风险与下周计划" },
    });
    fireEvent.change(screen.getByLabelText("输出要求"), {
      target: { value: "Markdown 周报，含进展/风险/计划三节" },
    });
    fireEvent.change(screen.getByLabelText("约束"), {
      target: { value: "只基于群内消息整理，不得虚构任何未提及的进展或风险" },
    });

    requestV2.mockResolvedValueOnce(
      ok({ entity: { id: SKILL_ID, status: "draft" }, revision: { revision_id: REV_ID } })
    );
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    const [path, options] = requestV2.mock.calls[0];    expect(path).toBe("/api/v2/skills");
    expect(options.method).toBe("POST");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toEqual({ ...CONTENT });
    expect("content_sha256" in options.body).toBe(false);
    expect("hash" in options.body).toBe(false);
  });

  it("400 VALIDATION_ERROR → formAlert 后端文案，弹窗保持", async () => {
    renderModal();
    await flush();

    for (const [label, value] of [
      ["名称", "周报整理"],
      ["描述", "把本周散乱记录整理成结构化周报"],
      ["AI 角色", "一名严谨的项目助理"],
      ["使用场景", "每周五需要把项目群里的进展整理成一份可发送的周报"],
      ["任务目标", "产出一份结构完整、重点突出、可直接发送给项目干系人的周报"],
      ["工作步骤", "先收集群消息，再按项目归类，最后提炼风险与下周计划"],
      ["输出要求", "Markdown 周报，含进展/风险/计划三节"],
      ["约束", "只基于群内消息整理，不得虚构任何未提及的进展或风险"],
    ]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    }

    requestV2.mockRejectedValueOnce(new V2ApiError("VALIDATION_ERROR", "name 长度不足", 400));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("name 长度不足");
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByRole("button", { name: "保存" })).toBeTruthy();
  });

  it("前端校验不通过 → 字段错误文案，不发请求", async () => {
    renderModal();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    expect(requestV2).not.toHaveBeenCalled();
    expect(screen.getByText("请填写名称")).toBeTruthy();
    expect(screen.getByText("请填写使用场景")).toBeTruthy();
  });
});

describe("编辑（PUT 全量替换）", () => {
  it("自取 detail 回填最新 revision content_json；PUT 九字段 body + 幂等键", async () => {
    await renderEditLoaded();

    expect(requestV2.mock.calls[0][0]).toBe(`/api/v2/skills/${SKILL_ID}`);
    expect(screen.getByLabelText("名称").value).toBe("周报整理");
    expect(screen.getByLabelText("约束").value).toBe(
      "只基于群内消息整理，不得虚构任何未提及的进展或风险"
    );

    fireEvent.change(screen.getByLabelText("描述"), {
      target: { value: "把本周散乱记录整理成可发送的结构化周报" },
    });
    requestV2.mockResolvedValueOnce(
      ok({ entity: { id: SKILL_ID, status: "draft" }, revision: { revision_id: REV_ID, revision_no: 1 } })
    );
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    const [path, options] = requestV2.mock.calls[1];
    expect(path).toBe(`/api/v2/skills/${SKILL_ID}`);
    expect(options.method).toBe("PUT");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toEqual({
      ...CONTENT,
      description: "把本周散乱记录整理成可发送的结构化周报",
    });
  });

  it("detail 装载失败 → formAlert，弹窗保持", async () => {
    requestV2.mockRejectedValueOnce(new V2ApiError("NOT_FOUND", "实体不存在", 404));
    renderModal(SKILL_ID);
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("实体不存在");
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});

describe("关闭行为", () => {
  it("Escape 关闭且不发请求；关闭回调触发", async () => {
    const onClose = vi.fn();
    render(<SkillEditorModal skillId={null} onClose={onClose} onSaved={vi.fn()} />);
    await flush();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(requestV2).not.toHaveBeenCalled();
  });
});
