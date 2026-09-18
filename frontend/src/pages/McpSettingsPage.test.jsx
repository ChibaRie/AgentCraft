import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext.jsx";
import { requestV2 } from "../api/v2/client.js";
import McpSettingsPage from "./McpSettingsPage.jsx";

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

const SERVER = {
  id: "aaaaaaaa-1111-1111-1111-111111111111",
  name: "Filesystem",
  transport_kind: "stdio",
  enabled: true,
  has_command: true,
  created_at: "2026-09-01T08:00:00",
};

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/settings/mcp"]}>
      <Routes>
        <Route path="/settings/mcp" element={<McpSettingsPage />} />
        <Route path="/profile" element={<div>个人中心标记</div>} />
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  useAuth.mockReturnValue({ v2User: { email: "v2@example.com" } });
  requestV2.mockRejectedValue(new Error("requestV2：本用例未显式编排"));
});

describe("MCP 管理页", () => {
  it("列出服务器（零命令泄漏）并可发现工具", async () => {
    requestV2.mockResolvedValueOnce(ok([SERVER]));
    renderPage();
    await flush();
    expect(screen.getByText("Filesystem")).toBeTruthy();
    expect(screen.getByText("stdio")).toBeTruthy();

    requestV2.mockResolvedValueOnce(ok({ tools: [{ tool_name: "echo" }] }));
    fireEvent.click(screen.getByRole("button", { name: "发现工具" }));
    await flush();
    expect(screen.getByText(/已发现 1 个工具：echo/)).toBeTruthy();
  });

  it("停用服务器走 PUT enabled=false", async () => {
    requestV2.mockResolvedValueOnce(ok([SERVER]));
    renderPage();
    await flush();
    requestV2.mockResolvedValueOnce(ok({ ...SERVER, enabled: false }));
    fireEvent.click(screen.getByRole("button", { name: "停用" }));
    await flush();
    expect(screen.getByRole("button", { name: "启用" })).toBeTruthy();
  });

  it("注册 stdio 服务器提交 command/args/env", async () => {
    requestV2.mockResolvedValueOnce(ok([]));
    renderPage();
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /注册 MCP 服务器/ }));
    await flush();
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "fs" } });
    fireEvent.change(screen.getByLabelText("启动命令"), { target: { value: "npx" } });
    fireEvent.change(screen.getByLabelText("参数（每行一个）"), {
      target: { value: "-y\n@modelcontextprotocol/server-filesystem" },
    });
    fireEvent.change(screen.getByLabelText("环境变量（每行 KEY=VALUE，值可选）"), {
      target: { value: "TOKEN=abc" },
    });
    requestV2.mockResolvedValueOnce(ok(SERVER));
    requestV2.mockResolvedValueOnce(ok([SERVER]));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await flush();
    const call = requestV2.mock.calls.find(([, options]) => options?.method === "POST");
    expect(call[1].body).toMatchObject({
      name: "fs",
      transport_kind: "stdio",
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-filesystem"],
      env: { TOKEN: "abc" },
    });
  });

  it("未登录不展示管理面", async () => {
    useAuth.mockReturnValue({ v2User: null });
    renderPage();
    await flush();
    expect(screen.getByText("MCP 管理需要新版账户会话。")).toBeTruthy();
  });
});
