import { act, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { request } from "../api/client.js";
import { requestV2 } from "../api/v2/client.js";
import { V2_DISCOVER } from "../api/v2/routes.js";
import ExpertCenterPage from "./ExpertCenterPage.jsx";

// 专家中心（Phase 8 T11 ①）：discover 调用切 V2_DISCOVER（page_size 语义对齐 V2），
// 信封 {items,total,page,page_size}；零 V1 调用。
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/client.js", () => ({ request: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

const V2_DISCOVER_PAGE = {
  items: [
    {
      id: "e-1",
      published_revision_id: "rev-1",
      name: "架构评审官",
      description: "评审系统设计",
      category: "engineering",
      skill_count: 2,
    },
    {
      id: "e-2",
      published_revision_id: "rev-2",
      name: "文案顾问",
      description: "打磨文案",
      category: "writing",
      skill_count: 0,
    },
  ],
  total: 11,
  page: 1,
  page_size: 9,
};

async function renderCenter(entry = "/discover") {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <ExpertCenterPage />
    </MemoryRouter>
  );
  await act(async () => {});
}

beforeEach(() => {
  vi.resetAllMocks();
  request.mockRejectedValue(new Error("V1 request：本用例未显式编排"));
  requestV2.mockResolvedValue({
    status: 200,
    data: V2_DISCOVER_PAGE,
    headers: new Headers(),
  });
  useAuth.mockReturnValue({ isAuthenticated: false, isExpert: false });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("discover 切 V2（T11 ①）", () => {
  it("请求 V2 discover 列表且分页参数为 page_size；V1 request 零调用", async () => {
    await renderCenter();

    expect(requestV2).toHaveBeenCalledWith(
      expect.stringMatching(new RegExp(`^${V2_DISCOVER}/experts\\?`))
    );
    const query = requestV2.mock.calls[0][0].split("?")[1];
    const params = new URLSearchParams(query);
    expect(params.get("page")).toBe("1");
    expect(params.get("page_size")).toBe("9");
    expect(params.get("search")).toBeNull();
    expect(params.get("category")).toBeNull();
    expect(request).not.toHaveBeenCalled();

    expect(screen.getByText("架构评审官")).toBeTruthy();
    expect(screen.getByText("文案顾问")).toBeTruthy();
    expect(screen.getByText(/共 11 位/)).toBeTruthy();
  });

  it("URL search/category 参数透传 V2 请求", async () => {
    await renderCenter("/discover?search=架构&category=engineering");

    const params = new URLSearchParams(requestV2.mock.calls[0][0].split("?")[1]);
    expect(params.get("search")).toBe("架构");
    expect(params.get("category")).toBe("engineering");
  });

  it("V2 失败 → 错误横幅（V2ApiError.message）", async () => {
    requestV2.mockRejectedValueOnce(
      Object.assign(new Error("请求失败（HTTP 500）"), { status: 500 })
    );
    await renderCenter();

    expect(screen.getByRole("alert").textContent).toBe("请求失败（HTTP 500）");
  });
});
