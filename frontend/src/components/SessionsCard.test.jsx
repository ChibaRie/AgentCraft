import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import SessionsCard from "./SessionsCard.jsx";

// 卡片级行为测试：mock useAuth（clearV2Session 观察点）+ requestV2 网络入口
vi.mock("../auth/AuthContext.jsx", () => ({ useAuth: vi.fn() }));
vi.mock("../api/v2/client.js", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, requestV2: vi.fn() };
});

function ok(data) {
  return { status: 200, data, headers: new Headers() };
}

/** 真实 V2ApiError 实例（组件以 instanceof 区分网络层裸错误的兜底路径） */
function v2Error(code, message, status, retryAfter) {
  return new V2ApiError(code, message, status, retryAfter);
}

/** 排空微任务队列（fireEvent 后异步 handler 的 setState 全部落定） */
function flush() {
  return act(async () => {});
}

// 后端 GET /auth/sessions 行形状（snake 原样；naive ISO 串）
const ROW_CURRENT = {
  id: "s-1",
  device_label: "Chrome / Windows",
  created_at: "2026-09-01T08:00:00",
  expires_at: "2026-09-15T08:00:00",
  current: true,
};
const ROW_OTHER = {
  id: "s-2",
  device_label: "Safari / macOS",
  created_at: "2026-08-30T10:30:00",
  expires_at: "2026-09-13T10:30:00",
  current: false,
};

/** 带路由观察点的渲染：/login 落地标记（撤销本机 → 跳 /login 的调用方职责） */
function renderCard() {
  return render(
    <MemoryRouter initialEntries={["/profile"]}>
      <Routes>
        <Route path="/profile" element={<SessionsCard />} />
        <Route path="/login" element={<div>登录页标记</div>} />
      </Routes>
    </MemoryRouter>
  );
}

/** 编排挂载列表并等待加载落定 */
async function renderWithSessions(rows) {
  requestV2.mockResolvedValueOnce(ok(rows));
  renderCard();
  await flush();
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({ clearV2Session: vi.fn() });
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("设备列表渲染", () => {
  it("挂载 GET /api/v2/auth/sessions，按 data 数组渲染行", async () => {
    await renderWithSessions([ROW_CURRENT, ROW_OTHER]);

    expect(requestV2).toHaveBeenCalledTimes(1);
    expect(requestV2.mock.calls[0][0]).toBe("/api/v2/auth/sessions");
    expect(screen.getByText("Chrome / Windows")).toBeTruthy();
    expect(screen.getByText("Safari / macOS")).toBeTruthy();
  });

  it("current 行挂「本机」badge + 按钮「仅退出本机」，其他行按钮「退出」", async () => {
    await renderWithSessions([ROW_CURRENT, ROW_OTHER]);

    expect(screen.getByText("本机")).toBeTruthy();
    expect(screen.getByRole("button", { name: "仅退出本机" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "退出" })).toBeTruthy();
  });

  it("device_label 缺失 → 行名回退「未知设备」；时间经 datetime 格式化", async () => {
    await renderWithSessions([
      { ...ROW_OTHER, device_label: null },
    ]);

    expect(screen.getByText("未知设备")).toBeTruthy();
    // naive ISO 串补 Z 后按本地时区 zh-CN 呈现（含年月日，不含原始 T/Z 记号）
    expect(screen.getByText(/2026\/8\/30/)).toBeTruthy();
    expect(screen.getByText(/2026\/9\/13/)).toBeTruthy();
  });
});

describe("退出会话流", () => {
  it("退出其他设备：DELETE /auth/sessions/{id} 携幂等键 → 200 → 静默刷新列表", async () => {
    await renderWithSessions([ROW_CURRENT, ROW_OTHER]);
    requestV2.mockResolvedValueOnce(ok({ ok: true })); // DELETE 200（先发）
    requestV2.mockResolvedValueOnce(ok([ROW_CURRENT])); // 刷新后列表：仅剩本机

    fireEvent.click(screen.getByRole("button", { name: "退出" }));
    await flush();

    expect(requestV2).toHaveBeenCalledTimes(3);
    const [path, options] = requestV2.mock.calls[1];
    expect(path).toBe("/api/v2/auth/sessions/s-2");
    expect(options.method).toBe("DELETE");
    expect(typeof options.idempotencyKey).toBe("string");
    expect(options.idempotencyKey.length).toBeGreaterThan(0);
    // 行已移除（刷新后仅本机）
    expect(screen.queryByText("Safari / macOS")).toBeNull();
    expect(screen.getByText("Chrome / Windows")).toBeTruthy();
    // 非本机退出不触发登出清理
    expect(useAuth().clearV2Session).not.toHaveBeenCalled();
  });

  it("仅退出本机：DELETE 200 → 清 V2 本地会话态 + 跳 /login（调用方负责）", async () => {
    await renderWithSessions([ROW_CURRENT]);
    requestV2.mockResolvedValueOnce(ok({ ok: true })); // DELETE 200

    fireEvent.click(screen.getByRole("button", { name: "仅退出本机" }));
    await flush();

    expect(requestV2.mock.calls[1][0]).toBe("/api/v2/auth/sessions/s-1");
    expect(useAuth().clearV2Session).toHaveBeenCalledTimes(1);
    expect(screen.getByText("登录页标记")).toBeTruthy();
  });

  it("DELETE 404（竞态：会话已被其他途径撤销）→ 静默刷新列表，不展示错误", async () => {
    await renderWithSessions([ROW_CURRENT, ROW_OTHER]);
    requestV2.mockRejectedValueOnce(v2Error("NOT_FOUND", "资源不存在", 404)); // DELETE 先发
    requestV2.mockResolvedValueOnce(ok([ROW_CURRENT])); // 静默刷新结果

    fireEvent.click(screen.getByRole("button", { name: "退出" }));
    await flush();

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("Safari / macOS")).toBeNull();
  });

  it("DELETE 其他失败（500）→ 内联后端文案，行保留", async () => {
    await renderWithSessions([ROW_CURRENT, ROW_OTHER]);
    requestV2.mockRejectedValueOnce(v2Error("INTERNAL", "服务暂不可用", 500));

    fireEvent.click(screen.getByRole("button", { name: "退出" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("服务暂不可用");
    expect(screen.getByText("Safari / macOS")).toBeTruthy();
  });

  it("网络层裸错误 → 兜底文案", async () => {
    await renderWithSessions([ROW_CURRENT, ROW_OTHER]);
    requestV2.mockRejectedValueOnce(new Error("network down"));

    fireEvent.click(screen.getByRole("button", { name: "退出" }));
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("操作失败，请稍后重试");
  });
});

describe("列表加载失败", () => {
  it("GET 失败 → 内联错误文案，不渲染行", async () => {
    requestV2.mockRejectedValueOnce(v2Error("INTERNAL", "服务暂不可用", 500));
    renderCard();
    await flush();

    expect(screen.getByRole("alert").textContent).toBe("服务暂不可用");
    expect(screen.queryByRole("button", { name: "退出" })).toBeNull();
  });
});
