# AgentCraft 脚手架搭建进度记录

**文档版本**：v0.2.0

**记录时间**：2026-09-02

**项目位置**：`C:\Users\ChibaRie\Desktop\AgentCraft\agentcraft`

**状态**：脚手架搭建完成，业务代码未实现

---

## 1. 本次目标

按以下文档搭建项目骨架：

- `AgentCraft_PRD_v0.4.0`
- `AgentCraft_Engineering_Spec_v0.4.0`
- `AgentCraft_Database_Design_v0.4.0`
- `AgentCraft_Scaffold_Plan_v0.4.0`

明确边界：

- 只搭建目录、依赖、配置、模型、迁移、路由、页面占位和测试骨架。
- 不实现登录、注册、专家管理、Skill 管理、任务对话、MCP 调用、Pi 编排、Provider Proxy 等业务逻辑。
- services 与 Pi/MCP 相关模块保留占位，API 未实现接口统一返回 `501`。

---

## 2. 已完成内容

### 2.1 后端

已完成：

- FastAPI 应用入口：`backend/main.py`
- 配置读取：`backend/config.py`
- 异步数据库引擎与会话工厂：`backend/database.py`
- SQLite 外键 PRAGMA：`PRAGMA foreign_keys=ON`
- 11 张业务表 ORM 模型
- Alembic 初始化、初始迁移与索引迁移
- API 路由与 Pydantic Schema 骨架
- 服务层占位
- Pi Engine 相关模块占位

已创建 ORM 表：

| 表名 | 用途 |
|---|---|
| `users` | 用户 |
| `experts` | 专家 |
| `skills` | Skill |
| `expert_skills` | 专家与 Skill 绑定 |
| `tasks` | 任务 |
| `conversations` | 会话 |
| `messages` | 消息 |
| `task_files` | 任务上传文件元数据 |
| `mcp_servers` | MCP Server |
| `mcp_tools` | MCP 工具 |
| `expert_mcps` | 专家与 MCP Server 绑定 |

已按 Database Design v0.4.0 补齐 11 个普通/复合索引：

| 索引名 | 表 | 索引列 |
|---|---|---|
| `idx_experts_owner_id` | `experts` | `owner_id` |
| `idx_experts_status_category` | `experts` | `status`, `category` |
| `idx_skills_owner_id` | `skills` | `owner_id` |
| `idx_tasks_user_id` | `tasks` | `user_id` |
| `idx_tasks_expert_id` | `tasks` | `expert_id` |
| `idx_messages_conv_created` | `messages` | `conversation_id`, `created_at` |
| `idx_task_files_task_created` | `task_files` | `task_id`, `created_at` |
| `idx_mcp_servers_owner_id` | `mcp_servers` | `owner_id` |
| `idx_mcp_tools_server_id` | `mcp_tools` | `server_id` |
| `idx_expert_skills_skill_id` | `expert_skills` | `skill_id` |
| `idx_expert_mcps_server_id` | `expert_mcps` | `server_id` |

索引迁移：`backend/alembic/versions/81c5eaa9f910_add_documented_indexes.py`。

### 2.2 API 骨架

已注册契约端点：

- 用户系统 API
- 专家管理 API
- 专家中心 API
- Skill 管理 API
- 任务与对话 API
- 任务文件 API
- MCP 管理 API
- Pi 内部回调 API
- 健康检查：`GET /api/health`

当前除健康检查外，业务端点均为占位：

```text
HTTP 501 Not Implemented
```

### 2.3 前端

已完成：

- React + Vite + React Router 骨架
- 原生 CSS 变量基础样式
- API Fetch 封装
- SSE 读取骨架
- P01-P09 页面占位

已注册路由：

```text
/login
/
/discover
/discover/{id}
/profile
/my-experts
/my-experts/new
/my-experts/{id}/edit
/skills
/tasks/{id}
```

### 2.4 Harness 与 Docker

已完成：

- Harness 上下文文件占位
- `validate_skill.py` 占位
- `check_code_style.py` 占位
- control 面 Dockerfile 骨架
- Pi worker Dockerfile 骨架
- Docker Compose 骨架

Docker Compose 预留：

- `control`
- `provider-proxy`
- `docker-socket-proxy`
- `pi-worker`

Pi 容器默认不启动。

### 2.5 测试

已完成基础测试骨架：

- `tests/test_health.py`
- `tests/test_models.py`
- `tests/test_migrations.py`
- `tests/test_api_contracts.py`
- `tests/test_pi_engine.py`
- `tests/test_pi_sandbox.py`

测试边界：

- 只验证健康检查、模型注册、迁移、API 契约存在性和占位行为。
- 不测试业务行为。

---

## 3. 验证记录

| 验证项 | 命令 | 结果 |
|---|---|---|
| Lint | `uv run ruff check .` | PASS |
| 后端测试 | `uv run pytest` | PASS，59 passed |
| 数据库迁移与索引 | `uv run alembic upgrade head` + `tests/test_migrations.py` | PASS |
| ORM 元数据一致性 | `uv run alembic check` | PASS，无新增迁移操作 |
| 索引回滚复验 | 索引迁移 upgrade/downgrade/re-upgrade | PASS |
| 前端构建 | `npm run build` | PASS |
| Docker Compose 配置 | `docker compose -f docker/docker-compose.yml config --quiet` | PASS |

---

## 4. 当前版本控制状态

- 分支：`main`
- 基线提交：`22dfceb AI: add AgentCraft v0.4.0 scaffold`
- 本次提交：数据库索引补齐与脚手架进度记录更新

---

## 5. 当前明确未完成

以下内容均未实现，留待正式开发阶段处理：

1. 用户注册、登录、JWT、权限。
2. 专家 CRUD、发布、下架、删除。
3. Skill CRUD、校验、发布、下架、快照。
4. 任务创建、消息流式回复、文件上传、任务状态机。
5. Pi 容器生命周期管理。
6. MCP Server 发现、工具启用、敏感授权、真实调用。
7. Provider Proxy。
8. 前端页面完整交互。
9. Docker 沙箱安全验证。
10. 反馈回路与循环检测。

---

## 6. 建议下一步

按文档顺序进入正式开发：

1. 用户认证与权限。
2. 专家与 Skill CRUD。
3. 任务、文件与会话管理。
4. Pi Agent RPC 集成。
5. Provider Proxy。
6. MCP 工具调用。
7. 前端完整交互。
8. 反馈回路与循环检测。
9. Docker 沙箱安全验证。

---

## 7. 变更记录

| 版本 | 时间 | 说明 |
|---|---|---|
| v0.1.0 | 2026-09-01 | 记录脚手架首版完成状态与验证结果 |
| v0.2.0 | 2026-09-02 | 补齐数据库文档定义的 11 个索引，新增 Alembic 索引迁移并扩展迁移测试 |
