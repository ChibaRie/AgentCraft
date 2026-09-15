/**
 * V2 admin 面客户端封装（Phase 8 T12a，七域——工程审查 I3）。
 *
 * 契约权威：Supplement v2.0.1 §6（admin 端点清单）+ §9.11 + §10.5（D8 读端点）；
 * 逐条对照勘察报告 `.superpowers/survey/phase8/phase8-admin-contracts.md` §2 端点表。
 * 挂载前缀 V2_ADMIN="/api/admin"（routes.js T0 预扩，cutover 不变——§10.1）。
 *
 * 七域：invitations / users / reviews / reports / catalog / audit / tasks-read，
 * 与 backend/api/v2/admin/* 路由文件一一映射（tasks-read=audit.py 产物下载 +
 * tasks.py 三端点，users 含 D8 用户任务列表）。
 *
 * 通用约束（Sup §6:126-128）：
 * - 写操作必带 Idempotency-Key 与 reason（幂等键由调用方参数化传入——用户触发
 *   的提交经 AdminReasonPrompt 生成，失败重试同键，语义 R3）；义务表见
 *   client.js V2_IDEMPOTENCY_REQUIRED（漏键 warn）。
 * - reason 原样参与 request_hash：同 key 异 reason → 409 IDEMPOTENCY_CONFLICT。
 * - 成功信封 {data}；列表 data={items,total,page,size}（1≤size≤100）。
 * - 读端点分层：元数据读免 reason；内容读（messages/files/产物下载）reason
 *   走查询参数必带（D9，对齐产物下载 #17 先例；URL 留痕 R7 登记接受）。
 */

import { requestV2 } from "./client.js";
import { V2_ADMIN } from "./routes.js";

/** 拼查询串：跳过 undefined/null/空串，其余 encodeURIComponent */
function qs(params) {
  const pairs = Object.entries(params).filter(
    ([, value]) => value !== undefined && value !== null && value !== ""
  );
  if (pairs.length === 0) {
    return "";
  }
  return `?${pairs.map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join("&")}`;
}

// ---------------------------------------------------------------------------
// 邀请域（backend/api/v2/admin/invitations.py）
// ---------------------------------------------------------------------------

/**
 * 创建邀请：POST /invitations。
 * 响应 201 {data:{id,email,expires_at}}——明文 token 只走邮件 outbox，
 * 响应/审计零 token（Sup:132），UI 绝不展示 token。
 */
export function createInvitation({ email, expiresInDays, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/invitations`, {
    method: "POST",
    body: { email, expires_in_days: expiresInDays, reason },
    idempotencyKey,
  });
}

/** 邀请列表：GET /invitations?status=&page=&size=（status ∈ open/consumed/expired/revoked） */
export function listInvitations({ status, page, size } = {}) {
  return requestV2(`${V2_ADMIN}/invitations${qs({ status, page, size })}`, {});
}

/** 撤销邀请：POST /invitations/{id}/revoke，载荷 {reason}（过期未消费可撤） */
export function revokeInvitation({ invitationId, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/invitations/${invitationId}/revoke`, {
    method: "POST",
    body: { reason },
    idempotencyKey,
  });
}

// ---------------------------------------------------------------------------
// 用户域（backend/api/v2/admin/users.py）
// ---------------------------------------------------------------------------

/** 用户列表：GET /users?email_prefix=&status=&page=&size=（元数据读） */
export function listUsers({ emailPrefix, status, page, size } = {}) {
  return requestV2(`${V2_ADMIN}/users${qs({ email_prefix: emailPrefix, status, page, size })}`, {});
}

/** 用户详情：GET /users/{id} → {user, quotas, usage, tasks:{total,by_status}}（不含任何 Key） */
export function getUserDetail(userId) {
  return requestV2(`${V2_ADMIN}/users/${userId}`, {});
}

/** 用户任务列表（D8 元数据读，§10.5）：GET /users/{id}/tasks?status=&page=&size= */
export function listUserTasks(userId, { status, page, size } = {}) {
  return requestV2(`${V2_ADMIN}/users/${userId}/tasks${qs({ status, page, size })}`, {});
}

/** 停用用户：POST /users/{id}/suspend，载荷 {reason}；级联回执 cascade|null（null=重放基线，R2 非告警） */
export function suspendUser({ userId, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/users/${userId}/suspend`, {
    method: "POST",
    body: { reason },
    idempotencyKey,
  });
}

/** 恢复用户：POST /users/{id}/unsuspend，载荷 {reason} */
export function unsuspendUser({ userId, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/users/${userId}/unsuspend`, {
    method: "POST",
    body: { reason },
    idempotencyKey,
  });
}

/**
 * 调整四维配额：PUT /users/{id}/quotas。quotas 键 camelCase
 * {maxDailyTasks?, maxActiveTasks?, maxRunningTasks?, maxRetainedStorageBytes?}，
 * 仅包含要调整的维度（后端 exclude_unset；strict int、负值/未知键 400）。
 */
export function updateUserQuotas({ userId, quotas, reason, idempotencyKey }) {
  const body = {
    ...(quotas.maxDailyTasks !== undefined && { max_daily_tasks: quotas.maxDailyTasks }),
    ...(quotas.maxActiveTasks !== undefined && { max_active_tasks: quotas.maxActiveTasks }),
    ...(quotas.maxRunningTasks !== undefined && { max_running_tasks: quotas.maxRunningTasks }),
    ...(quotas.maxRetainedStorageBytes !== undefined && {
      max_retained_storage_bytes: quotas.maxRetainedStorageBytes,
    }),
    reason,
  };
  return requestV2(`${V2_ADMIN}/users/${userId}/quotas`, {
    method: "PUT",
    body,
    idempotencyKey,
  });
}

/** 授予 entitlement：POST /users/{id}/entitlements，载荷 {kind, reason}（活跃冲突 409 ENTITLEMENT_ACTIVE） */
export function grantEntitlement({ userId, kind, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/users/${userId}/entitlements`, {
    method: "POST",
    body: { kind, reason },
    idempotencyKey,
  });
}

/**
 * 撤销 entitlement：DELETE /users/{id}/entitlements **带 JSON body** {kind, reason}
 * （D4a 裁决维持 DELETE+body，§10.7；0 行统一 404）。
 */
export function revokeEntitlement({ userId, kind, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/users/${userId}/entitlements`, {
    method: "DELETE",
    body: { kind, reason },
    idempotencyKey,
  });
}

// ---------------------------------------------------------------------------
// 审核域（backend/api/v2/admin/reviews.py）
// ---------------------------------------------------------------------------

/** 审核队列：GET /reviews?target_type=&page=&size=（target_type ∈ expert_revision/skill_revision） */
export function listReviews({ targetType, page, size } = {}) {
  return requestV2(`${V2_ADMIN}/reviews${qs({ target_type: targetType, page, size })}`, {});
}

/** 通过并发布：POST /reviews/{revision_id}/approve，载荷 {target_type, reason}（路径无 target_type） */
export function approveRevision({ revisionId, targetType, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/reviews/${revisionId}/approve`, {
    method: "POST",
    body: { target_type: targetType, reason },
    idempotencyKey,
  });
}

/** 驳回：POST /reviews/{revision_id}/reject，载荷 {target_type, reason} */
export function rejectRevision({ revisionId, targetType, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/reviews/${revisionId}/reject`, {
    method: "POST",
    body: { target_type: targetType, reason },
    idempotencyKey,
  });
}

// ---------------------------------------------------------------------------
// 举报域（backend/api/v2/admin/reports.py）
// ---------------------------------------------------------------------------

/** 举报队列（仅 open）：GET /reports?page=&size= */
export function listReports({ page, size } = {}) {
  return requestV2(`${V2_ADMIN}/reports${qs({ page, size })}`, {});
}

/** 处置举报：POST /reports/{id}/resolve，载荷 {action ∈ dismiss|takedown_revision|ban_author, reason} */
export function resolveReport({ reportId, action, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/reports/${reportId}/resolve`, {
    method: "POST",
    body: { action, reason },
    idempotencyKey,
  });
}

// ---------------------------------------------------------------------------
// 目录域（backend/api/v2/admin/catalog.py，含 kill-switch）
// ---------------------------------------------------------------------------

/** Provider 目录（含停用条目，§10.8 转正）：GET /catalog/providers */
export function listCatalogProviders() {
  return requestV2(`${V2_ADMIN}/catalog/providers`, {});
}

/** Provider 目录项启停/模型白名单整表替换：PUT /catalog/providers/{id}，载荷 {enabled?, models?, reason} */
export function updateCatalogProvider({ providerId, enabled, models, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/catalog/providers/${providerId}`, {
    method: "PUT",
    body: { ...(enabled !== undefined && { enabled }), ...(models !== undefined && { models }), reason },
    idempotencyKey,
  });
}

/** 平台工具目录（label 未登记组合回退 tool_id）：GET /catalog/tools */
export function listCatalogTools() {
  return requestV2(`${V2_ADMIN}/catalog/tools`, {});
}

/** 平台工具启停：PUT /catalog/tools，载荷 {tool_id, version, enabled, reason} */
export function updateCatalogTool({ toolId, version, enabled, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/catalog/tools`, {
    method: "PUT",
    body: { tool_id: toolId, version, enabled, reason },
    idempotencyKey,
  });
}

/**
 * 全局 kill switch（单版本粒度）：POST /tools/{tool_id}/kill-switch，载荷 {version, reason}。
 * 限流 admin_kill_switch 10/h/用户：429 + Retry-After（重放不耗窗，R4）；
 * 响应 termination|null——null=重放基线/executor 缺位（R2 非告警）。
 */
export function killSwitchTool({ toolId, version, reason, idempotencyKey }) {
  return requestV2(`${V2_ADMIN}/tools/${toolId}/kill-switch`, {
    method: "POST",
    body: { version, reason },
    idempotencyKey,
  });
}

// ---------------------------------------------------------------------------
// 审计域（backend/api/v2/admin/audit.py）
// ---------------------------------------------------------------------------

/**
 * 审计查询：GET /audit-logs（六维过滤 + 分页；since/until ISO 8601，
 * naive 视为 UTC 双端闭区间；created_at DESC+id DESC 稳定序）。
 */
export function listAuditLogs({ actorId, action, targetType, targetId, since, until, page, size } = {}) {
  const query = qs({
    actor_id: actorId,
    action,
    target_type: targetType,
    target_id: targetId,
    since,
    until,
    page,
    size,
  });
  return requestV2(`${V2_ADMIN}/audit-logs${query}`, {});
}

/**
 * admin 产物下载直链（FileResponse，非 JSON 信封——不经 requestV2）：
 * `GET /tasks/{task_id}/artifacts/{file_id}/download?reason=`。reason 必带且走
 * URL 查询串（R7 登记接受）：调用方以弹窗收集 reason、构造 <a href>/fetch blob
 * 直链下载，下载后不留存。T12b 审计页消费。
 */
export function buildArtifactDownloadUrl({ taskId, fileId, reason }) {
  return `${V2_ADMIN}/tasks/${taskId}/artifacts/${fileId}/download${qs({ reason })}`;
}

// ---------------------------------------------------------------------------
// 任务读域（backend/api/v2/admin/tasks.py，Sup §10.5 D8——内容读先审计后读）
// ---------------------------------------------------------------------------

/**
 * admin 消息读（内容读）：GET /tasks/{id}/messages?after=&limit=&reason=。
 * reason 必带（缺/空白 400 ADMIN_REASON_REQUIRED）；默认 50 上限 200，
 * event_sequence 升序全量正文。
 */
export function listTaskMessagesAdmin(taskId, { after, limit, reason } = {}) {
  return requestV2(`${V2_ADMIN}/tasks/${taskId}/messages${qs({ after, limit, reason })}`, {});
}

/**
 * admin 文件元数据读（内容读）：GET /tasks/{id}/files?direction=&reason=。
 * 仅元数据 file_name/sha256/size/state（无内容字节）；direction ∈ input|output。
 */
export function listTaskFilesAdmin(taskId, { direction, reason } = {}) {
  return requestV2(`${V2_ADMIN}/tasks/${taskId}/files${qs({ direction, reason })}`, {});
}

/** admin 任务快照（元数据读，免 reason 免审计）：GET /tasks/{id}（owner 面同形 + §10.3 展示字段） */
export function getTaskAdmin(taskId) {
  return requestV2(`${V2_ADMIN}/tasks/${taskId}`, {});
}
