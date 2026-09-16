import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../../auth/AuthContext.jsx";
import { requestV2, V2ApiError } from "../../api/v2/client.js";
import RequireAdmin from "../../components/RequireAdmin.jsx";
import CatalogPage from "./CatalogPage.jsx";

// 目录与 kill-switch 页（Phase 8 T12b，测试清单③④）：providers 开关 + models
// 白名单编辑器（整表替换心智提示）；tools 启停（label 由后端回退 tool_id，前端
// 原样渲染）；kill-switch 危险壳（单版本确认 + reason + 429 Retry-After 倒计时
// 复用 useRetryAfter + termination 回执 null 非失败——R2/R4）。
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
        <CatalogPage />
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

const PROVIDERS = {
  items: [
    {
      id: "p1",
      display_name: "OpenAI 兼容",
      allowed_host: "api.openai.com",
      models: ["gpt-4o", "gpt-4o-mini"],
      enabled: true,
    },
  ],
  total: 1,
};

const TOOLS = {
  items: [
    {
      tool_id: "check_code_style",
      version: "1.0.0",
      label: "代码风格检查",
      enabled: true,
      permissions: ["filesystem:read"],
    },
    {
      // label 未登记组合：后端回退 tool_id，前端原样渲染
      tool_id: "mystery_tool",
      version: "0.9.0",
      label: "mystery_tool",
      enabled: false,
      permissions: [],
    },
  ],
  total: 2,
};

const REASON_LABEL = "操作原因（必填，审计留痕）";

const FULL_CATALOG = {
  "GET /api/admin/catalog/providers": () => ok(PROVIDERS),
  "GET /api/admin/catalog/tools": () => ok(TOOLS),
};

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue(ADMIN_CTX);
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("目录装载", () => {
  it("挂载拉取 providers+tools；label 原样渲染（含回退 tool_id 的条目）", async () => {
    stubByPath(FULL_CATALOG);
    renderPage();
    await flush();

    expect(requestV2).toHaveBeenCalledWith("/api/admin/catalog/providers", expect.anything());
    expect(requestV2).toHaveBeenCalledWith("/api/admin/catalog/tools", expect.anything());
    expect(screen.getByText("代码风格检查")).toBeTruthy();
    // 回退条目：label 与 tool_id 同串，label 列 + tool_id 列各出现一次
    expect(screen.getAllByText("mystery_tool").length).toBe(2);
    expect(screen.getByText("api.openai.com")).toBeTruthy();
  });
});

describe("provider 启停与白名单", () => {
  it("启停：PUT /catalog/providers/{id} 载荷 {enabled, reason} + 幂等键", async () => {
    stubByPath({
      ...FULL_CATALOG,
      "PUT /api/admin/catalog/providers/p1": () =>
        ok({ id: "p1", display_name: "OpenAI 兼容", allowed_host: "api.openai.com", models: ["gpt-4o"], enabled: false }),
    });
    renderPage();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "停用 Provider" }));
    await flush();
    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "供应商下线" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    });
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/catalog/providers/p1",
      expect.objectContaining({
        method: "PUT",
        body: { enabled: false, reason: "供应商下线" },
        idempotencyKey: expect.any(String),
      })
    );
  });

  it("models 白名单编辑器：整表替换提示 + PUT {models, reason}", async () => {
    stubByPath({
      ...FULL_CATALOG,
      "PUT /api/admin/catalog/providers/p1": () =>
        ok({ id: "p1", display_name: "OpenAI 兼容", allowed_host: "api.openai.com", models: ["gpt-4o"], enabled: true }),
    });
    renderPage();
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "编辑白名单" }));
    const editor = within(screen.getByRole("dialog"));
    expect(editor.getByText(/整表替换/)).toBeTruthy();
    const textarea = editor.getByLabelText("模型白名单（每行一个）");
    expect(textarea.value).toBe("gpt-4o\ngpt-4o-mini");
    fireEvent.change(textarea, { target: { value: "gpt-4o\no4-mini" } });
    fireEvent.click(editor.getByRole("button", { name: "下一步" }));
    await flush();

    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "白名单收敛" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    });
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/catalog/providers/p1",
      expect.objectContaining({
        method: "PUT",
        body: { models: ["gpt-4o", "o4-mini"], reason: "白名单收敛" },
        idempotencyKey: expect.any(String),
      })
    );
  });
});

describe("tool 启停", () => {
  it("PUT /catalog/tools 载荷 {tool_id, version, enabled, reason}", async () => {
    stubByPath({
      ...FULL_CATALOG,
      "PUT /api/admin/catalog/tools": () =>
        ok({ tool_id: "check_code_style", version: "1.0.0", enabled: false, already_in_state: false }),
    });
    renderPage();
    await flush();

    const row = screen.getByText("代码风格检查").closest("tr");
    fireEvent.click(within(row).getByRole("button", { name: "停用" }));
    await flush();
    fireEvent.change(screen.getByLabelText(REASON_LABEL), { target: { value: "工具存在缺陷" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    });
    await flush();

    expect(requestV2).toHaveBeenCalledWith(
      "/api/admin/catalog/tools",
      expect.objectContaining({
        method: "PUT",
        body: { tool_id: "check_code_style", version: "1.0.0", enabled: false, reason: "工具存在缺陷" },
        idempotencyKey: expect.any(String),
      })
    );
  });
});

describe("③④kill-switch 危险壳", () => {
  it("版本确认不匹配时确认按钮禁用（单版本粒度防误触）", async () => {
    stubByPath(FULL_CATALOG);
    renderPage();
    await flush();
    const row = screen.getByText("代码风格检查").closest("tr");
    fireEvent.click(within(row).getByRole("button", { name: "Kill Switch" }));
    const dialog = within(screen.getByRole("dialog"));

    expect(dialog.getByRole("button", { name: "确认执行" }).disabled).toBe(true);
    fireEvent.change(dialog.getByLabelText("版本号确认"), { target: { value: "0.9.0" } });
    fireEvent.change(dialog.getByLabelText(REASON_LABEL), { target: { value: "紧急止血" } });
    expect(dialog.getByRole("button", { name: "确认执行" }).disabled).toBe(true);
    fireEvent.change(dialog.getByLabelText("版本号确认"), { target: { value: "1.0.0" } });
    expect(dialog.getByRole("button", { name: "确认执行" }).disabled).toBe(false);
    expect(requestV2).not.toHaveBeenCalledWith(
      "/api/admin/tools/check_code_style/kill-switch",
      expect.anything()
    );
  });

  it("429 → Retry-After 倒计时禁用确认按钮，归零后可重试（同键）", async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      const { act: actFromLib } = await import("@testing-library/react");
      stubByPath({
        ...FULL_CATALOG,
        "POST /api/admin/tools/check_code_style/kill-switch": () => {
          calls += 1;
          if (calls === 1) {
            return Promise.reject(new V2ApiError("RATE_LIMITED", "操作过于频繁", 429, 2));
          }
          return Promise.resolve(
            ok({
              tool_id: "check_code_style",
              version: "1.0.0",
              enabled: false,
              already_in_state: false,
              termination: { stopped: 1, aborted_task_ids: ["t1"], receipts: [] },
            })
          );
        },
      });
      renderPage();
      await actFromLib(async () => {});
      const row = screen.getByText("代码风格检查").closest("tr");
      fireEvent.click(within(row).getByRole("button", { name: "Kill Switch" }));
      const dialog = screen.getByRole("dialog");
      fireEvent.change(within(dialog).getByLabelText("版本号确认"), { target: { value: "1.0.0" } });
      fireEvent.change(within(dialog).getByLabelText(REASON_LABEL), { target: { value: "紧急止血" } });

      await actFromLib(async () => {
        fireEvent.click(within(dialog).getByRole("button", { name: "确认执行" }));
      });
      await actFromLib(async () => {});

      // 429：倒计时渲染 + 确认禁用（弹窗保持，输入保留）
      expect(within(dialog).getByRole("alert").textContent).toContain("操作过于频繁");
      expect(dialog.textContent).toContain("2 秒");
      expect(within(dialog).getByRole("button", { name: "确认执行" }).disabled).toBe(true);

      await actFromLib(async () => {
        vi.advanceTimersByTime(2000);
      });
      await actFromLib(async () => {});
      expect(within(dialog).getByRole("button", { name: "确认执行" }).disabled).toBe(false);

      await actFromLib(async () => {
        fireEvent.click(within(dialog).getByRole("button", { name: "确认执行" }));
      });
      await actFromLib(async () => {});

      expect(calls).toBe(2);
      // 两次提交同键同载荷（R3/R4 重试语义）；成功后另有目录刷新 GET，
      // 故用 toHaveBeenCalledWith 而非 lastCalledWith
      expect(requestV2).toHaveBeenCalledWith(
        "/api/admin/tools/check_code_style/kill-switch",
        expect.objectContaining({
          method: "POST",
          body: { version: "1.0.0", reason: "紧急止血" },
          idempotencyKey: expect.any(String),
        })
      );
      // termination 回执：非 null → 计数渲染
      expect(screen.getByText(/停止执行：1/)).toBeTruthy();
      expect(screen.getByText(/终止任务：1/)).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("termination=null（executor 缺位/重放基线）→ 非失败提示，零告警（R2/R4）", async () => {
    let calls = 0;
    stubByPath({
      ...FULL_CATALOG,
      "POST /api/admin/tools/check_code_style/kill-switch": () => {
        calls += 1;
        return Promise.resolve(
          ok({
            tool_id: "check_code_style",
            version: "1.0.0",
            enabled: false,
            already_in_state: false,
            termination: null,
          })
        );
      },
    });
    renderPage();
    await flush();
    const row = screen.getByText("代码风格检查").closest("tr");
    fireEvent.click(within(row).getByRole("button", { name: "Kill Switch" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("版本号确认"), { target: { value: "1.0.0" } });
    fireEvent.change(within(dialog).getByLabelText(REASON_LABEL), { target: { value: "紧急止血" } });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole("button", { name: "确认执行" }));
    });
    await flush();

    expect(calls).toBe(1);
    expect(screen.getByText(/仅停用目录条目/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
