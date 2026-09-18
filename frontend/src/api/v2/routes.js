/**
 * V2 API 契约路径常量（FE-T1 产出，T2-T9 消费）。
 * Phase 8 T14 cutover：全部值按 Sup §10.1 路径切换总表自实现期 /api/v2 暂挂
 * 前缀切至契约路径（backend/main.py: v2_api_router 挂载于 /api）；
 * 此后前端一律经本单点消费，不再出现 /api/v2 字面路径。
 */
export const V2_AUTH = "/api/auth";
export const V2_ACCOUNT = "/api/account";
export const V2_USERS = "/api/users";
export const V2_PROVIDERS = "/api/providers";
export const V2_MCP_SERVERS = "/api/mcp/servers";

// —— Phase 8 T0 一次性预扩全（T8-T12b 只导入，不修改本文件）：T14 cutover 已切契约路径。
export const V2_TASKS = "/api/tasks";
export const V2_DISCOVER = "/api/discover";
export const V2_AUTHORING_EXPERTS = "/api/experts";
export const V2_AUTHORING_SKILLS = "/api/skills";
export const V2_ADMIN = "/api/admin";
