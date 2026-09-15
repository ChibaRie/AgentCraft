/**
 * V2 API 路由前缀常量（FE-T1 产出，T2-T9 消费）。
 * 与 backend/api/v2 各 router 的挂载点一一对应（backend/main.py: /api/v2 前缀）。
 * 注意：V2_USERS 当前尚无后端 router（T5+ 落地），常量先行钉死契约。
 */
export const V2_AUTH = "/api/v2/auth";
export const V2_ACCOUNT = "/api/v2/account";
export const V2_USERS = "/api/v2/users";
export const V2_PROVIDERS = "/api/v2/providers";

// —— Phase 8 T0 一次性预扩全（T8-T12b 只导入，不修改本文件）：值即当前挂载；Phase 8 T14 cutover 仅改值。
export const V2_TASKS = "/api/v2/tasks";
export const V2_QUOTA = "/api/v2/quota";
export const V2_DISCOVER = "/api/v2/discover";
export const V2_AUTHORING_EXPERTS = "/api/v2/experts";
export const V2_AUTHORING_SKILLS = "/api/v2/skills";
export const V2_REPORTS = "/api/v2/reports";
export const V2_ADMIN = "/api/admin";
