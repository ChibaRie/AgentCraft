import { describe, expect, it, vi, afterEach } from "vitest";
import { V2_IDEMPOTENCY_REQUIRED, requestV2 } from "./client.js";

/** 可编程响应（client.test.js 同款最小形态） */
function jsonResponse(body, { status = 200 } = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** vi.stubGlobal + mockImplementationOnce 队列（client.test.js 同款） */
function stubFetch(...responses) {
  const fetchMock = vi.fn();
  for (const response of responses) {
    fetchMock.mockImplementationOnce(() => Promise.resolve(response));
  }
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/**
 * 幂等义务表 admin 面追加（Phase 8 T12a，测试清单⑦）：13 个写端点
 * （Sup §6 + §10.5；entitlements POST/DELETE 同路径共用一模式，故 12 条模式）
 * 命中义务表——漏传 idempotencyKey 时 console.warn 生效（R10）。
 * V1/V2 既有 8 模式回归由 client.test.js 钉死，本文件只测 admin 增量。
 */
describe("V2_IDEMPOTENCY_REQUIRED · admin 13 写端点覆盖（⑦）", () => {
  it.each([
    ["POST", "/api/admin/invitations"],
    ["POST", "/api/admin/invitations/i1/revoke"],
    ["POST", "/api/admin/users/u1/suspend"],
    ["POST", "/api/admin/users/u1/unsuspend"],
    ["PUT", "/api/admin/users/u1/quotas"],
    ["POST", "/api/admin/users/u1/entitlements"],
    ["DELETE", "/api/admin/users/u1/entitlements"],
    ["POST", "/api/admin/reviews/r1/approve"],
    ["POST", "/api/admin/reviews/r1/reject"],
    ["POST", "/api/admin/reports/rp1/resolve"],
    ["PUT", "/api/admin/catalog/providers/p1"],
    ["PUT", "/api/admin/catalog/tools"],
    ["POST", "/api/admin/tools/check_code_style/kill-switch"],
  ])("%s %s 命中义务表", (_method, path) => {
    expect(V2_IDEMPOTENCY_REQUIRED.some((re) => re.test(path))).toBe(true);
  });

  it("admin 读端点（元数据读/内容读）不命中义务表——模式不误伤", () => {
    const readPaths = [
      "/api/admin/users",
      "/api/admin/users/u1",
      "/api/admin/users/u1/tasks",
      "/api/admin/invitations/i1", // 防御：详情类路径不在义务表
      "/api/admin/reviews",
      "/api/admin/reports",
      "/api/admin/audit-logs",
      "/api/admin/catalog/providers",
      "/api/admin/tasks/t1",
      "/api/admin/tasks/t1/messages",
      "/api/admin/tasks/t1/files",
      "/api/admin/tasks/t1/artifacts/f1/download",
    ];
    for (const path of readPaths) {
      expect(V2_IDEMPOTENCY_REQUIRED.some((re) => re.test(path))).toBe(false);
    }
  });

  it("admin 写端点漏键 → console.warn 实际触发（R10 行为钉）", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    stubFetch(jsonResponse({ data: {} }));
    try {
      await requestV2("/api/admin/users/u1/suspend", { method: "POST", body: { reason: "x" } });
      expect(warnSpy).toHaveBeenCalledTimes(1);
      expect(String(warnSpy.mock.calls[0][0])).toContain("/api/admin/users/u1/suspend");
    } finally {
      warnSpy.mockRestore();
    }
  });

  it("幂等键显式提供 → 无 warn 且注入 Idempotency-Key 头", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const fetchMock = stubFetch(jsonResponse({ data: {} }));
    try {
      await requestV2("/api/admin/users/u1/suspend", {
        method: "POST",
        body: { reason: "x" },
        idempotencyKey: "admin-key-1",
      });
      expect(warnSpy).not.toHaveBeenCalled();
      const headers = fetchMock.mock.calls[0][1].headers;
      expect(headers["Idempotency-Key"]).toBe("admin-key-1");
    } finally {
      warnSpy.mockRestore();
    }
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});
