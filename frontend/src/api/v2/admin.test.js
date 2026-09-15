import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  buildArtifactDownloadUrl,
  createInvitation,
  approveRevision,
  getTaskAdmin,
  getUserDetail,
  grantEntitlement,
  killSwitchTool,
  listAuditLogs,
  listCatalogProviders,
  listCatalogTools,
  listInvitations,
  listReports,
  listReviews,
  listTaskFilesAdmin,
  listTaskMessagesAdmin,
  listUserTasks,
  listUsers,
  rejectRevision,
  resolveReport,
  revokeEntitlement,
  revokeInvitation,
  suspendUser,
  unsuspendUser,
  updateCatalogProvider,
  updateCatalogTool,
  updateUserQuotas,
} from "./admin.js";

// admin.js 七域封装测试（Phase 8 T12a，工程审查 I3）：逐条对照勘察报告
// phase8-admin-contracts §2 端点表 + Sup §10.5 D8 四端点——方法/路径/查询串/
// 载荷（snake_case）/幂等键透传。requestV2 打桩观察，网络层行为由 client.test.js 钉死。
vi.mock("./client.js", () => ({ requestV2: vi.fn() }));

import { requestV2 } from "./client.js";

const KEY = "idem-test-key";

function expectCall(method, path, options = {}) {
  expect(requestV2).toHaveBeenCalledTimes(1);
  const [calledPath, calledOptions = {}] = requestV2.mock.calls[0];
  expect(calledPath).toBe(path);
  // GET 封装可不传 options（requestV2 默认 method="GET"）
  expect(calledOptions.method ?? "GET").toBe(method);
  for (const [key, value] of Object.entries(options)) {
    expect(calledOptions[key]).toEqual(value);
  }
}

beforeEach(() => {
  vi.resetAllMocks();
});

describe("邀请域（invitations）", () => {
  it("createInvitation：POST /invitations，expiresInDays→expires_in_days，幂等键透传", async () => {
    requestV2.mockResolvedValue({ status: 201, data: { id: "i1" } });
    await createInvitation({
      email: "new@example.com",
      expiresInDays: 3,
      reason: "新增成员",
      idempotencyKey: KEY,
    });
    expectCall("POST", "/api/admin/invitations", {
      body: { email: "new@example.com", expires_in_days: 3, reason: "新增成员" },
      idempotencyKey: KEY,
    });
  });

  it("listInvitations：带过滤参数时拼查询串；空参时不带 ?", async () => {
    await listInvitations({ status: "open", page: 2, size: 10 });
    expectCall("GET", "/api/admin/invitations?status=open&page=2&size=10");

    requestV2.mockClear();
    await listInvitations({});
    expectCall("GET", "/api/admin/invitations");
  });

  it("revokeInvitation：POST /invitations/{id}/revoke，载荷仅 reason", async () => {
    await revokeInvitation({ invitationId: "i1", reason: "误建", idempotencyKey: KEY });
    expectCall("POST", "/api/admin/invitations/i1/revoke", {
      body: { reason: "误建" },
      idempotencyKey: KEY,
    });
  });
});

describe("用户域（users，含 D8 用户任务列表）", () => {
  it("listUsers：email_prefix/status/page/size 查询串", async () => {
    await listUsers({ emailPrefix: "ali", status: "active", page: 1, size: 20 });
    expectCall("GET", "/api/admin/users?email_prefix=ali&status=active&page=1&size=20");
  });

  it("getUserDetail：GET /users/{id}", async () => {
    await getUserDetail("u1");
    expectCall("GET", "/api/admin/users/u1");
  });

  it("listUserTasks（D8 元数据读）：GET /users/{id}/tasks?status=&page=&size=", async () => {
    await listUserTasks("u1", { status: "running", page: 1, size: 20 });
    expectCall("GET", "/api/admin/users/u1/tasks?status=running&page=1&size=20");
  });

  it("suspendUser/unsuspendUser：POST 载荷仅 reason", async () => {
    await suspendUser({ userId: "u1", reason: "违规", idempotencyKey: KEY });
    expectCall("POST", "/api/admin/users/u1/suspend", {
      body: { reason: "违规" },
      idempotencyKey: KEY,
    });

    requestV2.mockClear();
    await unsuspendUser({ userId: "u1", reason: "申诉通过", idempotencyKey: KEY });
    expectCall("POST", "/api/admin/users/u1/unsuspend", {
      body: { reason: "申诉通过" },
      idempotencyKey: KEY,
    });
  });

  it("updateUserQuotas：PUT /users/{id}/quotas，仅包含提供的维度（缺省不发 null）", async () => {
    await updateUserQuotas({
      userId: "u1",
      quotas: { maxDailyTasks: 5, maxRetainedStorageBytes: 1024 },
      reason: "扩容",
      idempotencyKey: KEY,
    });
    expectCall("PUT", "/api/admin/users/u1/quotas", {
      body: { max_daily_tasks: 5, max_retained_storage_bytes: 1024, reason: "扩容" },
      idempotencyKey: KEY,
    });
  });

  it("grantEntitlement：POST /users/{id}/entitlements 载荷 {kind, reason}", async () => {
    await grantEntitlement({
      userId: "u1",
      kind: "expert_author",
      reason: "作者准入",
      idempotencyKey: KEY,
    });
    expectCall("POST", "/api/admin/users/u1/entitlements", {
      body: { kind: "expert_author", reason: "作者准入" },
      idempotencyKey: KEY,
    });
  });

  it("revokeEntitlement（D4a）：DELETE 带 JSON body {kind, reason} + 幂等键", async () => {
    await revokeEntitlement({
      userId: "u1",
      kind: "expert_author",
      reason: "撤销资格",
      idempotencyKey: KEY,
    });
    expectCall("DELETE", "/api/admin/users/u1/entitlements", {
      body: { kind: "expert_author", reason: "撤销资格" },
      idempotencyKey: KEY,
    });
  });
});

describe("审核域（reviews）", () => {
  it("listReviews：target_type/page/size 查询串", async () => {
    await listReviews({ targetType: "expert_revision", page: 1, size: 20 });
    expectCall("GET", "/api/admin/reviews?target_type=expert_revision&page=1&size=20");
  });

  it("approveRevision：POST /reviews/{revision_id}/approve，target_type 走 body（路径无 target_type）", async () => {
    await approveRevision({
      revisionId: "r1",
      targetType: "skill_revision",
      reason: "内容合规",
      idempotencyKey: KEY,
    });
    expectCall("POST", "/api/admin/reviews/r1/approve", {
      body: { target_type: "skill_revision", reason: "内容合规" },
      idempotencyKey: KEY,
    });
  });

  it("rejectRevision：POST /reviews/{revision_id}/reject", async () => {
    await rejectRevision({
      revisionId: "r1",
      targetType: "expert_revision",
      reason: "描述不实",
      idempotencyKey: KEY,
    });
    expectCall("POST", "/api/admin/reviews/r1/reject", {
      body: { target_type: "expert_revision", reason: "描述不实" },
      idempotencyKey: KEY,
    });
  });
});

describe("举报域（reports）", () => {
  it("listReports：仅分页参数", async () => {
    await listReports({ page: 1, size: 20 });
    expectCall("GET", "/api/admin/reports?page=1&size=20");
  });

  it("resolveReport：POST /reports/{id}/resolve 载荷 {action, reason}", async () => {
    await resolveReport({
      reportId: "rp1",
      action: "ban_author",
      reason: "多次违规",
      idempotencyKey: KEY,
    });
    expectCall("POST", "/api/admin/reports/rp1/resolve", {
      body: { action: "ban_author", reason: "多次违规" },
      idempotencyKey: KEY,
    });
  });
});

describe("目录域（catalog + kill-switch）", () => {
  it("listCatalogProviders：GET /catalog/providers（契约外转正端点，§10.8）", async () => {
    await listCatalogProviders();
    expectCall("GET", "/api/admin/catalog/providers");
  });

  it("updateCatalogProvider：PUT /catalog/providers/{id}，载荷 enabled/models/reason", async () => {
    await updateCatalogProvider({
      providerId: "p1",
      enabled: false,
      models: ["deepseek-chat"],
      reason: "供应商下线",
      idempotencyKey: KEY,
    });
    expectCall("PUT", "/api/admin/catalog/providers/p1", {
      body: { enabled: false, models: ["deepseek-chat"], reason: "供应商下线" },
      idempotencyKey: KEY,
    });
  });

  it("listCatalogTools：GET /catalog/tools", async () => {
    await listCatalogTools();
    expectCall("GET", "/api/admin/catalog/tools");
  });

  it("updateCatalogTool：PUT /catalog/tools，载荷 {tool_id, version, enabled, reason}", async () => {
    await updateCatalogTool({
      toolId: "check_code_style",
      version: "1.0.0",
      enabled: false,
      reason: "工具缺陷",
      idempotencyKey: KEY,
    });
    expectCall("PUT", "/api/admin/catalog/tools", {
      body: { tool_id: "check_code_style", version: "1.0.0", enabled: false, reason: "工具缺陷" },
      idempotencyKey: KEY,
    });
  });

  it("killSwitchTool：POST /tools/{tool_id}/kill-switch，载荷 {version, reason}", async () => {
    await killSwitchTool({
      toolId: "check_code_style",
      version: "1.0.0",
      reason: "紧急止损",
      idempotencyKey: KEY,
    });
    expectCall("POST", "/api/admin/tools/check_code_style/kill-switch", {
      body: { version: "1.0.0", reason: "紧急止损" },
      idempotencyKey: KEY,
    });
  });
});

describe("审计域（audit）", () => {
  it("listAuditLogs：六维过滤 + 分页查询串", async () => {
    await listAuditLogs({
      actorId: "a1",
      action: "user.suspend",
      targetType: "user",
      targetId: "u1",
      since: "2026-09-01T00:00:00",
      until: "2026-09-02T00:00:00",
      page: 1,
      size: 50,
    });
    expectCall(
      "GET",
      "/api/admin/audit-logs?actor_id=a1&action=user.suspend&target_type=user&target_id=u1" +
        "&since=2026-09-01T00%3A00%3A00&until=2026-09-02T00%3A00%3A00&page=1&size=50"
    );
  });

  it("buildArtifactDownloadUrl：reason 走查询参数（URL 编码；§6 #17 先例）", () => {
    expect(buildArtifactDownloadUrl({ taskId: "t1", fileId: "f1", reason: "举报核查" })).toBe(
      "/api/admin/tasks/t1/artifacts/f1/download?reason=" + encodeURIComponent("举报核查")
    );
  });
});

describe("任务读域（tasks-read，Sup §10.5 D8）", () => {
  it("listTaskMessagesAdmin：GET /tasks/{id}/messages?after=&limit=&reason=", async () => {
    await listTaskMessagesAdmin("t1", { after: 5, limit: 50, reason: "举报核查" });
    expectCall(
      "GET",
      `/api/admin/tasks/t1/messages?after=5&limit=50&reason=${encodeURIComponent("举报核查")}`
    );
  });

  it("listTaskFilesAdmin：GET /tasks/{id}/files?direction=&reason=", async () => {
    await listTaskFilesAdmin("t1", { direction: "input", reason: "举报核查" });
    expectCall(
      "GET",
      `/api/admin/tasks/t1/files?direction=input&reason=${encodeURIComponent("举报核查")}`
    );
  });

  it("getTaskAdmin：GET /tasks/{id}（元数据读，免 reason）", async () => {
    await getTaskAdmin("t1");
    expectCall("GET", "/api/admin/tasks/t1");
  });
});
