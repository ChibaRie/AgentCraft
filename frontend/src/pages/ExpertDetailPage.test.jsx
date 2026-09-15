import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { requestV2 } from "../api/v2/client.js";
import { V2_DISCOVER } from "../api/v2/routes.js";
import ExpertDetailPage from "./ExpertDetailPage.jsx";

// 专家详情（Phase 8 T11 ①）：detail 切 V2 discover（Sup §10.2 published_revision_id），
// 「召唤专家」携 expert + rid 查询参数（rid 缺失 → 仅 expert，创建页回退选择流）。
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useNavigate: vi.fn() };
});
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

const navigateMock = vi.fn();

const V2_DETAIL = {
  id: "e-1",
  published_revision_id: "rev-9",
  name: "架构评审官",
  description: "评审系统设计",
  category: "engineering",
  persona: "严谨",
  methodology: "清单式",
  task_examples: ["评审一份架构文档"],
  skills: [{ skill_id: "s-1", name: "架构清单", revision_no: 2 }],
  tools: [],
};

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

async function renderDetail() {
  render(
    <MemoryRouter>
      <ExpertDetailPage expertId="e-1" />
    </MemoryRouter>
  );
  await act(async () => {});
}

beforeEach(() => {
  vi.resetAllMocks();
  useNavigate.mockReturnValue(navigateMock);
  requestV2.mockResolvedValue(ok(V2_DETAIL));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("detail 切 V2 discover（T11 ①）", () => {
  it("请求 V2 详情并渲染人设/方法论/Skill", async () => {
    await renderDetail();

    expect(requestV2).toHaveBeenCalledWith(`${V2_DISCOVER}/experts/e-1`);
    expect(screen.getByText("架构评审官")).toBeTruthy();
    expect(screen.getByText("严谨")).toBeTruthy();
    expect(screen.getByText("架构清单")).toBeTruthy();
  });

  it("召唤专家 → /tasks/new?expert={id}&rid={published_revision_id}（简报 Step 2 ④）", async () => {
    await renderDetail();
    fireEvent.click(screen.getByRole("button", { name: "召唤专家" }));

    expect(navigateMock).toHaveBeenCalledWith("/tasks/new?expert=e-1&rid=rev-9");
  });

  it("published_revision_id 缺失（防御）→ 仅携 expert 参数，创建页回退选择流", async () => {
    requestV2.mockResolvedValue(
      ok({ ...V2_DETAIL, published_revision_id: undefined })
    );
    await renderDetail();
    fireEvent.click(screen.getByRole("button", { name: "召唤专家" }));

    expect(navigateMock).toHaveBeenCalledWith("/tasks/new?expert=e-1");
  });

  it("V2 404 → 专家不存在或未公开", async () => {
    requestV2.mockRejectedValue(
      Object.assign(new Error("专家不存在"), { code: "NOT_FOUND", status: 404 })
    );
    await renderDetail();

    expect(screen.getByText("专家不存在或未公开")).toBeTruthy();
  });
});
