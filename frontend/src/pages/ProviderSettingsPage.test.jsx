import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import ProviderSettingsPage from "./ProviderSettingsPage.jsx";

// 页面级行为测试：mock useAuth（v2User gating 观察点）+ requestV2 网络入口
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

/** 真实 V2ApiError 实例（组件以 instanceof/status/code 分流） */
function v2Error(code, message, status, retryAfter) {
  return new V2ApiError(code, message, status, retryAfter);
}

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定） */
function flush() {
  return act(async () => {});
}

// 后端 GET /providers/catalog 行形状（snake 原样；仅 enabled）
const CATALOG = [
  {
    id: "c".repeat(32),
    display_name: "DeepSeek",
    allowed_host: "api.deepseek.com",
    models: ["deepseek-chat", "deepseek-reasoner"],
  },
  {
    id: "d".repeat(32),
    display_name: "Moonshot",
    allowed_host: "api.moonshot.cn",
    models: ["kimi-k2"],
  },
];

// 后端 GET /providers 行形状（ProviderOut，D13 字段清单；仅 active、created_at asc）
const PROVIDER_A = {
  id: "aaaaaaaa-1111-1111-1111-111111111111",
  catalog_id: CATALOG[0].id,
  catalog_display_name: "DeepSeek",
  model_id: "deepseek-chat",
  key_last4: "ab12",
  key_version: 1,
  status: "active",
  is_default: true,
  created_at: "2026-09-01T08:00:00",
};
const PROVIDER_B = {
  id: "bbbbbbbb-2222-2222-2222-222222222222",
  catalog_id: CATALOG[1].id,
  catalog_display_name: "Moonshot",
  model_id: "kimi-k2",
  key_last4: "cd34",
  key_version: 2,
  status: "active",
  is_default: false,
  created_at: "2026-09-02T08:00:00",
};
/** POST 200 后 refetch 回来的新行（嵌入列表断言刷新真的发生） */
const NEW_ROW = {
  id: "cccccccc-3333-3333-3333-333333333333",
  catalog_id: CATALOG[0].id,
  catalog_display_name: "DeepSeek",
  model_id: "deepseek-reasoner",
  key_last4: "ff88",
  key_version: 1,
  status: "active",
  is_default: false,
  created_at: "2026-09-03T08:00:00",
};

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/settings/providers"]}>
      <Routes>
        <Route path="/settings/providers" element={<ProviderSettingsPage />} />
        <Route path="/profile" element={<div>个人中心标记</div>} />
      </Routes>
    </MemoryRouter>
  );
}

/** 编排挂载（catalog + providers 两个 GET）并等待加载落定 */
async function renderWithProviders(rows = [PROVIDER_A, PROVIDER_B]) {
  requestV2.mockResolvedValueOnce(ok(CATALOG));
  requestV2.mockResolvedValueOnce(ok(rows));
  renderPage();
  await flush();
}

/** 打开添加表单 */
async function openAddForm() {
  fireEvent.click(screen.getByRole("button", { name: "添加 Provider" }));
  await flush();
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({ v2User: { email: "v2@example.com" } });
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("v2User gating 与数据装载", () => {
  it("无 v2User → 不渲染页面内容、不发任何请求", async () => {
    useAuth.mockReturnValue({ v2User: null });
    renderPage();
    await flush();

    expect(requestV2).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "添加 Provider" })).toBeNull();
  });

  it("挂载并行 GET catalog + providers，卡片渲染字段钉死", async () => {
    await renderWithProviders();

    expect(requestV2).toHaveBeenCalledTimes(2);
    expect(requestV2.mock.calls[0][0]).toBe("/api/providers/catalog");
    expect(requestV2.mock.calls[1][0]).toBe("/api/providers");

    expect(screen.getByText("DeepSeek")).toBeTruthy();
    expect(screen.getByText("Moonshot")).toBeTruthy();
    expect(screen.getByText("deepseek-chat")).toBeTruthy();
    expect(screen.getByText("••••ab12")).toBeTruthy();
    expect(screen.getByText("v1")).toBeTruthy();
    expect(screen.getByText("v2")).toBeTruthy();
    expect(screen.getByText("默认")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "测试连通性" }).length).toBe(2);
    expect(screen.getAllByRole("button", { name: "更换 Key" }).length).toBe(2);
    expect(screen.getAllByRole("button", { name: "删除" }).length).toBe(2);
    // 仅非默认卡显示「设为默认」
    expect(screen.getAllByRole("button", { name: "设为默认" }).length).toBe(1);
  });

  it("GET 失败 → 内联错误文案，不渲染行", async () => {
    requestV2.mockResolvedValueOnce(ok(CATALOG));
    requestV2.mockRejectedValueOnce(v2Error("INTERNAL", "服务暂不可用", 500));
    renderPage();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("服务暂不可用");
    expect(screen.queryByText("••••ab12")).toBeNull();
  });
});

describe("添加 Provider", () => {
  it("表单无 base_url/protocol 字段；api_key 为 password 型且可切换显示", async () => {
    await renderWithProviders();
    await openAddForm();

    expect(screen.queryByLabelText("Base URL")).toBeNull();
    expect(screen.queryByLabelText("协议")).toBeNull();

    const keyInput = screen.getByLabelText("API Key");
    expect(keyInput.type).toBe("password");
    fireEvent.click(screen.getByRole("button", { name: "显示" }));
    expect(screen.getByLabelText("API Key").type).toBe("text");
  });

  it("目录→模型联动：目录推荐项进 datalist，未选目录时模型输入禁用（白名单退役为建议项）", async () => {
    await renderWithProviders();
    await openAddForm();

    const catalogSelect = screen.getByLabelText("目录条目");
    const modelInput = screen.getByLabelText("模型（可自定义）");
    expect(modelInput.disabled).toBe(true);

    fireEvent.change(catalogSelect, { target: { value: CATALOG[0].id } });
    expect(modelInput.disabled).toBe(false);
    const datalist = document.getElementById("provider-model-options");
    expect(
      Array.from(datalist.querySelectorAll("option")).map((option) => option.value)
    ).toEqual(["deepseek-chat", "deepseek-reasoner"]);

    fireEvent.change(catalogSelect, { target: { value: CATALOG[1].id } });
    expect(
      Array.from(document.getElementById("provider-model-options").querySelectorAll("option")).map(
        (option) => option.value
      )
    ).toEqual(["kimi-k2"]);
  });

  it("提交幂等键 → 200 后整体 refetch（列表顺序来源单一），表单关闭", async () => {
    await renderWithProviders();
    await openAddForm();
    fireEvent.change(screen.getByLabelText("目录条目"), {
      target: { value: CATALOG[0].id },
    });
    fireEvent.change(screen.getByLabelText("模型（可自定义）"), {
      target: { value: "deepseek-chat" },
    });
    fireEvent.change(screen.getByLabelText("API Key"), {
      target: { value: "sk-test-1234" },
    });
    fireEvent.click(screen.getByLabelText("设为我的默认（建任务时默认选中）"));

    requestV2.mockResolvedValueOnce(ok(NEW_ROW)); // POST 200（先发）
    requestV2.mockResolvedValueOnce(ok([PROVIDER_A, NEW_ROW])); // refetch 列表
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    expect(requestV2).toHaveBeenCalledTimes(4);
    const [path, options] = requestV2.mock.calls[2];
    expect(path).toBe("/api/providers");
    expect(options.method).toBe("POST");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toEqual({
      catalog_id: CATALOG[0].id,
      model_id: "deepseek-chat",
      api_key: "sk-test-1234",
      is_default: true,
    });
    // 整体 refetch：第 4 次调用是 GET providers（refreshProviders 单参调用，无 method 选项）
    expect(requestV2.mock.calls[3][0]).toBe("/api/providers");
    expect(requestV2.mock.calls[3][1]?.method).toBeUndefined();
    expect(screen.getByText("••••ff88")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
  });

  it("默认开关未勾选 → POST body 无 is_default 键（缺席语义）", async () => {
    await renderWithProviders();
    await openAddForm();
    fireEvent.change(screen.getByLabelText("目录条目"), {
      target: { value: CATALOG[1].id },
    });
    fireEvent.change(screen.getByLabelText("模型（可自定义）"), {
      target: { value: "kimi-k2" },
    });
    fireEvent.change(screen.getByLabelText("API Key"), {
      target: { value: "sk-test-5678" },
    });

    requestV2.mockResolvedValueOnce(ok(NEW_ROW));
    requestV2.mockResolvedValueOnce(ok([PROVIDER_A, PROVIDER_B, NEW_ROW]));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    const [, options] = requestV2.mock.calls[2];
    expect(options.body).toEqual({
      catalog_id: CATALOG[1].id,
      model_id: "kimi-k2",
      api_key: "sk-test-5678",
    });
    expect("is_default" in options.body).toBe(false);
  });

  it("409 PROVIDER_DUPLICATE → 内联后端文案；失败后重试生成新幂等键", async () => {
    await renderWithProviders();
    await openAddForm();
    fireEvent.change(screen.getByLabelText("目录条目"), {
      target: { value: CATALOG[0].id },
    });
    fireEvent.change(screen.getByLabelText("模型（可自定义）"), {
      target: { value: "deepseek-chat" },
    });
    fireEvent.change(screen.getByLabelText("API Key"), {
      target: { value: "sk-test-1234" },
    });

    requestV2.mockRejectedValueOnce(
      v2Error("PROVIDER_DUPLICATE", "已存在相同目录与模型的 Provider", 409)
    );
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe(
      "已存在相同目录与模型的 Provider"
    );
    expect(screen.getByRole("button", { name: "保存" })).toBeTruthy();
    const firstKey = requestV2.mock.calls[2][1].idempotencyKey;

    // 失败后再次提交：新幂等键 + 200 成功收口
    requestV2.mockResolvedValueOnce(ok(NEW_ROW));
    requestV2.mockResolvedValueOnce(ok([PROVIDER_A, NEW_ROW]));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    const secondKey = requestV2.mock.calls[3][1].idempotencyKey;
    expect(secondKey).toBeTruthy();
    expect(secondKey).not.toBe(firstKey);
    expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
  });

  it("400 目录类错误（CATALOG_ITEM_DISABLED）→ 内联后端文案 + 提示刷新目录", async () => {
    await renderWithProviders();
    await openAddForm();
    fireEvent.change(screen.getByLabelText("目录条目"), {
      target: { value: CATALOG[0].id },
    });
    fireEvent.change(screen.getByLabelText("模型（可自定义）"), {
      target: { value: "deepseek-chat" },
    });
    fireEvent.change(screen.getByLabelText("API Key"), {
      target: { value: "sk-test-1234" },
    });

    requestV2.mockRejectedValueOnce(
      v2Error("CATALOG_ITEM_DISABLED", "目录条目已停用", 400)
    );
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("目录条目已停用");
    expect(alert.textContent).toContain("刷新");
  });
});

describe("测试连通性", () => {
  it("200 ok → 「正常 · {latency}ms · 可见 {n} 个模型」；POST 无幂等键（D10）", async () => {
    await renderWithProviders();
    requestV2.mockResolvedValueOnce(ok({ ok: true, latency_ms: 123, models_visible: 7 }));

    fireEvent.click(screen.getAllByRole("button", { name: "测试连通性" })[0]);
    await flush();

    const [path, options] = requestV2.mock.calls[2];
    expect(path).toBe(`/api/providers/${PROVIDER_A.id}/test`);
    expect(options.method).toBe("POST");
    expect(options.idempotencyKey).toBeUndefined();
    expect(screen.getByText("正常 · 123ms · 可见 7 个模型")).toBeTruthy();
  });

  it("200 ok:false → 「连接失败（{latency}ms）」内联展示", async () => {
    await renderWithProviders();
    requestV2.mockResolvedValueOnce(ok({ ok: false, latency_ms: 450, models_visible: 0 }));

    fireEvent.click(screen.getAllByRole("button", { name: "测试连通性" })[1]);
    await flush();

    expect(screen.getByText("连接失败（450ms）")).toBeTruthy();
  });

  it("点击后按钮进入 loading 旋转态并禁用", async () => {
    await renderWithProviders();
    requestV2.mockImplementationOnce(() => new Promise(() => {})); // 永不落定

    fireEvent.click(screen.getAllByRole("button", { name: "测试连通性" })[0]);
    await flush();

    const loadingButton = screen.getByRole("button", { name: "测试中…" });
    expect(loadingButton.disabled).toBe(true);
  });

  it("400 KEY_VERSION_REVOKED → 固定文案 + 引导聚焦轮换表单", async () => {
    await renderWithProviders();
    requestV2.mockRejectedValueOnce(
      v2Error("KEY_VERSION_REVOKED", "Provider Key 不可用或已失效", 400)
    );

    fireEvent.click(screen.getAllByRole("button", { name: "测试连通性" })[0]);
    await flush();

    expect(screen.getByText("Provider Key 不可用或已失效，请轮换 Key")).toBeTruthy();
    const rotateInput = screen.getByLabelText("新 API Key");
    expect(rotateInput).toBeTruthy();
    expect(document.activeElement).toBe(rotateInput);
  });

  it("429 → 按钮禁用 + 倒计时提示，归零自动解除", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    await renderWithProviders();
    requestV2.mockRejectedValueOnce(
      v2Error("RATE_LIMITED", "请求过于频繁", 429, 30)
    );

    fireEvent.click(screen.getAllByRole("button", { name: "测试连通性" })[0]);
    // 429 分支后 useRetryAfter 启动 interval：假计时器下排空微任务
    await act(async () => {});

    expect(
      screen.getByRole("status").textContent
    ).toBe("测试次数已达上限（每小时 10 次），30s 后可重试");
    expect(screen.getAllByRole("button", { name: "测试连通性" })[0].disabled).toBe(
      true
    );

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole("status").textContent).toContain("28");

    act(() => {
      vi.advanceTimersByTime(28000);
    });
    expect(screen.getAllByRole("button", { name: "测试连通性" })[0].disabled).toBe(
      false
    );
  });
});

describe("轮换（更换 Key）", () => {
  it("PUT 幂等键 + body 只含非空字段 → 200 就地更新（key_last4/key_version/默认徽标）", async () => {
    await renderWithProviders();
    fireEvent.click(screen.getAllByRole("button", { name: "更换 Key" })[1]);
    await flush();
    fireEvent.change(screen.getByLabelText("新 API Key"), {
      target: { value: "sk-rotated-9999" },
    });
    fireEvent.click(screen.getByLabelText("设为默认"));

    requestV2.mockResolvedValueOnce(
      ok({ ...PROVIDER_B, key_last4: "zz99", key_version: 3, is_default: true })
    );
    fireEvent.click(screen.getByRole("button", { name: "保存更改" }));
    await flush();

    const [path, options] = requestV2.mock.calls[2];
    expect(path).toBe(`/api/providers/${PROVIDER_B.id}`);
    expect(options.method).toBe("PUT");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toEqual({ api_key: "sk-rotated-9999", is_default: true });
    // 就地更新，无额外 GET
    expect(requestV2).toHaveBeenCalledTimes(3);
    expect(screen.getByText("••••zz99")).toBeTruthy();
    expect(screen.getByText("v3")).toBeTruthy();
    // 默认徽标移到 B 卡（本地互斥镜像），A 卡原徽标消失
    const badge = screen.getByText("默认");
    expect(badge.closest("li").textContent).toContain("Moonshot");
    expect(badge.closest("li").textContent).not.toContain("DeepSeek");
  });

  it("api_key 留空且默认开关未勾选 → body 两键整体缺席（缺席=不变）", async () => {
    await renderWithProviders();
    fireEvent.click(screen.getAllByRole("button", { name: "更换 Key" })[0]);
    await flush();

    requestV2.mockResolvedValueOnce(ok(PROVIDER_A));
    fireEvent.click(screen.getByRole("button", { name: "保存更改" }));
    await flush();

    const [, options] = requestV2.mock.calls[2];
    expect(options.method).toBe("PUT");
    // 契约钉死：两键均缺席（显式 false 会静默清默认位；null 会 400）
    expect("api_key" in options.body).toBe(false);
    expect("is_default" in options.body).toBe(false);
  });

  it("PUT 失败 → 内联后端文案，表单保持打开", async () => {
    await renderWithProviders();
    fireEvent.click(screen.getAllByRole("button", { name: "更换 Key" })[0]);
    await flush();
    fireEvent.change(screen.getByLabelText("新 API Key"), {
      target: { value: "sk-short" },
    });

    requestV2.mockRejectedValueOnce(
      v2Error("VALIDATION_ERROR", "api_key 长度不足", 400)
    );
    fireEvent.click(screen.getByRole("button", { name: "保存更改" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("api_key 长度不足");
    expect(screen.getByRole("button", { name: "保存更改" })).toBeTruthy();
    expect(screen.getByText("••••ab12")).toBeTruthy();
  });
});

describe("设为默认", () => {
  it("PUT {is_default: true} 幂等键 → 200 就地更新徽标，无 refetch", async () => {
    await renderWithProviders();
    requestV2.mockResolvedValueOnce(ok({ ...PROVIDER_B, is_default: true }));

    fireEvent.click(screen.getByRole("button", { name: "设为默认" }));
    await flush();

    const [path, options] = requestV2.mock.calls[2];
    expect(path).toBe(`/api/providers/${PROVIDER_B.id}`);
    expect(options.method).toBe("PUT");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.body).toEqual({ is_default: true });
    expect(requestV2).toHaveBeenCalledTimes(3);

    const badge = screen.getByText("默认");
    expect(badge.closest("li").textContent).toContain("Moonshot");
    expect(screen.getAllByRole("button", { name: "设为默认" }).length).toBe(1);
  });
});

describe("删除（软撤）", () => {
  it("确认弹层文案逐字 + DELETE 幂等键 → 200 移出列表", async () => {
    await renderWithProviders();
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[1]);
    await flush();

    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(
      screen.getByText(/撤销后该 Provider 立即失效，关联的未开始任务将被终止/)
    ).toBeTruthy();

    requestV2.mockResolvedValueOnce(ok({ id: PROVIDER_B.id, status: "revoked" }));
    fireEvent.click(screen.getByRole("button", { name: "确认撤销" }));
    await flush();

    const [path, options] = requestV2.mock.calls[2];
    expect(path).toBe(`/api/providers/${PROVIDER_B.id}`);
    expect(options.method).toBe("DELETE");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(screen.queryByText("••••cd34")).toBeNull();
    expect(screen.getByText("••••ab12")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("DELETE 404（竞态：已被其他途径撤销）→ 静默刷新，不展示错误", async () => {
    await renderWithProviders();
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[1]);
    await flush();
    requestV2.mockRejectedValueOnce(v2Error("NOT_FOUND", "资源不存在", 404));
    requestV2.mockResolvedValueOnce(ok([PROVIDER_A]));

    fireEvent.click(screen.getByRole("button", { name: "确认撤销" }));
    await flush();

    expect(screen.queryByRole("alert")).toBeNull();
    expect(requestV2.mock.calls[3][0]).toBe("/api/providers");
    expect(screen.queryByText("••••cd34")).toBeNull();
  });

  it("DELETE 其他失败（500）→ 弹层内联后端文案，行保留", async () => {
    await renderWithProviders();
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[1]);
    await flush();
    requestV2.mockRejectedValueOnce(v2Error("INTERNAL", "服务暂不可用", 500));

    fireEvent.click(screen.getByRole("button", { name: "确认撤销" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("服务暂不可用");
    expect(screen.getByText("••••cd34")).toBeTruthy();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });

  it("取消关闭弹层，不发请求", async () => {
    await renderWithProviders();
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await flush();

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(requestV2).toHaveBeenCalledTimes(2);
  });
});
