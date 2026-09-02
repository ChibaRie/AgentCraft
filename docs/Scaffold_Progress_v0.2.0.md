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

## 7. 阶段 1：用户系统垂直切片（2026-09-02 完成）

按 PRD v0.4.1 §4.1、Engineering Spec v0.4.0 §6.2 实现第一个垂直切片。

### 7.1 后端（TDD：tests/test_users.py 先行，23 个用例）

| 端点 | 行为 |
|---|---|
| `POST /api/auth/register` | 201 `{data:{id,username,email,role,token}}`；400 校验失败（信封 `VALIDATION_ERROR`）；409 `USERNAME_EXISTS`/`EMAIL_EXISTS`；注册即自动登录 |
| `POST /api/auth/login` | 200 同上；`login` 接受用户名或邮箱；401 `INVALID_CREDENTIALS`（统一提示"账号或密码不正确"，防账号枚举） |
| `GET /api/users/me` | 200 `{data:{id,username,email,role,created_at}}`；无/坏 token 401 `UNAUTHORIZED` |
| `POST /api/users/me/expert` | 200 role→expert；重复申请 409 `ALREADY_EXPERT` |

配套变更：

- JWT（python-jose，HS256，2h）+ passlib/bcrypt（bcrypt 锁 4.0.1 适配 passlib 1.7.4）
- `main.py` 全局异常处理器：所有失败统一 `{error:{code,message}}` 信封；`RequestValidationError` 转 400（规格 §6.1）
- `middleware/auth.py`：`get_current_user`（加载 User 实体）与 `get_current_user_id`（占位端点沿用）
- `middleware/permission.py`：`require_expert_role`（403 `FORBIDDEN`），供阶段 2 专家管理接口使用
- 修复脚手架缺陷：`User` 模型缺失 4 个反向 relationship（Expert/Skill/MCPServer/Task 的 `back_populates` 悬空导致 mapper 配置失败）
- `tests/conftest.py`：每测试独立 SQLite（tmp 文件 + NullPool）+ `get_db` 依赖覆盖
- `tests/test_api_contracts.py`：已实现端点移出 501 断言；受保护占位端点匿名请求断言 401

### 7.2 前端（React 18 + Vite，原生 CSS tokens）

- 设计语言「墨与纸」：黑白灰、中等对比、发丝线 + 灰阶渐变 + 分层阴影；支持 `prefers-color-scheme` 暗色；`prefers-reduced-motion` 降级
- `auth/AuthContext.jsx`：Token 持久化 localStorage（`agentcraft_token`），挂载时经 `/api/users/me` 恢复会话，401 自动清除
- `api/client.js`：Bearer 注入 + `{data}`/`{error}` 信封解析
- P01 登录/注册页：分栏墨纸构图、滑动墨块切换、PRD 文案的内联校验（去空格计长、两次密码一致）、服务端冲突映射到字段
- NavBar + UserMenu：权限感知菜单（专家用户才显示我的专家/Skill 管理）、Escape/外点关闭
- P05 个人中心：账号信息、专家身份申请（即时生效 + 成功态）、任务列表空态
- 路由守卫：`RequireAuth`（未登录跳 /login 并记录 from），`requireExpert` 分支；P01 对已登录用户回跳

### 7.3 验证记录

| 验证项 | 结果 |
|---|---|
| `uv run pytest` | 78 passed, 4 skipped |
| `uv run ruff check .` | PASS |
| `uv run alembic check` | PASS（relationship 变更不影响表结构） |
| `npm run build` | PASS（gzip 70KB） |
| Playwright 端到端 | 注册→自动登录→个人中心→申请专家→登出→守卫→邮箱重登 全通过 |
| 截图 | `.impeccable/review/`（桌面/移动、亮色/暗色、普通/专家态） |

---

## 8. 阶段 2：Skill 管理垂直切片（2026-09-02 完成）

按 PRD v0.4.1 §4.4、Engineering Spec v0.4.0 §6.5/§9.1、DB 设计 §5.3 实现完整 Skill 生命周期。

### 8.1 validate_skill 校验器（harness/mcp/validate_skill.py，30 个单测）

纯文本规则校验，无外部依赖、不执行输入内容：

| 检查项 | 级别 |
|---|---|
| 必填完整性（role/goal/steps/output_requirements/constraints） | ERROR |
| 字段长度 ≤5000 字符 | WARNING |
| API Key 模式（sk-/ghp_/AKIA/xox/AIza） | ERROR |
| 危险指令模式（rm -rf、mkfs、dd、del /s、盘符格式化、DROP TABLE、管道执行脚本） | ERROR |
| 可执行代码模式（import os/subprocess、eval/exec、os.system） | WARNING |
| 越狱模板（[INST]、`<<<`、`</system>`、忽略以上所有指令、现在你是一个，§7.5） | WARNING 但标记不通过 |

### 8.2 Skill API（§6.5 全契约，31 个 API 测试）

| 端点 | 行为 |
|---|---|
| `POST /api/skills` | 201 draft；PRD §4.4.2 字段规则（name 2-30 去空格、description 10-200 去空格、role 5-200、长文本 20-5000 非全空白、input_requirements 可选 ≤5000） |
| `GET /api/skills` | 分页信封 {data,total,page,size}，仅本人，创建时间倒序 |
| `GET /api/skills/{id}` | 详情含 bound_experts；他人 403、缺失 404 |
| `PUT /api/skills/{id}` | 部分更新；published 必须同事务通过 validate_skill，失败 400 `SKILL_INVALID` 并回滚保留原内容 |
| `POST .../publish` | draft/offline→published；内容不过校验 400；重复发布 409 |
| `POST .../offline` | 仅 published 可下架，否则 409 |
| `POST .../validate` | 只读校验已保存内容，不改状态 |
| `DELETE /api/skills/{id}` | 前置已解绑，否则 409 `SKILL_STILL_BOUND` |

全部端点专家身份门禁（未登录 401 / 普通用户 403 `FORBIDDEN`）。状态机严格按 DB 设计 §5.3。

### 8.3 前端 P08（Skill 部分）

- 列表卡片：状态徽标（草稿描边/已发布墨底/已下架虚线）、校验、发布/下架、编辑、绑定情况、删除（行内二次确认）
- SkillEditorModal：9 字段弹窗（PRD 提示文案与错误文案）、客户端校验与服务端错误内联呈现
- ValidateButton 行内校验报告：通过态与 issues 列表（ERROR/WARNING 分级徽标）
- 骨架屏加载、空态引导、MCP Server 标签页占位（后续阶段）

### 8.4 验证记录

| 验证项 | 结果 |
|---|---|
| `uv run pytest` | 135 passed, 12 skipped（新增 30 校验单测 + 31 API 测试） |
| `uv run ruff check .` | PASS |
| `npm run build` | PASS（gzip 74KB） |
| Playwright 端到端 | 创建 → validate → 发布 → 下架全流程；已发布编辑塞入 API Key → 400 保留原内容，弹窗内呈现校验失败信息 |

---

## 9. 阶段 3：专家 CRUD 与绑定 + 专家中心（2026-09-02 完成，闭环一收口）

按 PRD §4.2、Engineering Spec §6.3/§6.4、DB 设计 §5.2 实现。MCP 绑定端点保留 501，随 MCP 管理阶段实现。

### 9.1 专家管理 API（§6.3，38 个测试）

| 端点 | 行为 |
|---|---|
| `POST /api/experts` | 201 draft；PRD §4.2.2 字段规则（name 2-30 去空格、description 10-100 去空格、category 枚举、persona/methodology 非空白不设上限、task_examples ≤5 条每条 ≤50 字符、avatar_url 仅 http/https 格式校验） |
| `GET /api/experts` | 分页信封；`?status=draft,published,offline` 逗号分隔过滤；仅本人 |
| `GET /api/experts/{id}` | 详情含 skills（含 enabled/status）与 mcps（空，随 MCP 阶段接入）；不返回 MCP 连接信息；他人 403 / 缺失 404 |
| `PUT /api/experts/{id}` | 部分更新；task_examples 缺席不改、显式 null 清空 |
| `POST .../publish` | 前置：至少一个 published+enabled 绑定，否则 400 `EXPERT_PUBLISH_CONDITION`；重复发布 409 |
| `POST .../offline` | 仅 published 可下架；running 任务回收随任务阶段接入 |
| `DELETE /api/experts/{id}` | 前置无任何状态任务引用（快照规则），否则 409 `EXPERT_STILL_REFERENCED`；级联删绑定、不影响 Skill |
| `POST /{id}/skills` | 绑定默认 enabled=false；Skill 必须 published（400 `SKILL_NOT_PUBLISHED`）；他人 Skill 403；重复 409；enabled=true 防御性内容校验 |
| `PUT /{id}/skills/{skill_id}` | 开关绑定；enabled=true 重查 published+校验；404 `BINDING_NOT_FOUND`（绑定优先于归属） |
| `DELETE /{id}/skills/{skill_id}` | 解绑；解绑后可重绑 |

### 9.2 专家中心 API（§6.4，匿名可访问）

- `GET /api/discover/experts`：仅 published；`?search`（LIKE 转义 `%_\`）/`?category`/分页；卡片含 skill_count
- `GET /api/discover/experts/{id}`：公开详情（人设/方法论/任务示例/启用 Skill）；非 published 404
- 公开口径统一为「enabled 绑定 + Skill 当前 published」：离线 Skill（kill switch）不出现也不计数，卡片与详情一致

### 9.3 前端 P03/P04/P06/P07

- P06 我的专家：状态徽标、发布/下架、编辑入口、删除二次确认（409 提示任务引用）
- P07 专家编辑：双栏（表单 + SkillBindingPanel）；任务示例动态增删（≤5）；绑定下拉只列已发布未绑定 Skill；启用/关闭开关（墨色滑块）；保存后 create→edit 导航
- P03 专家中心：搜索 + 分类 chips + 分页（URL 状态驱动）；骨架屏/空态
- P04 专家详情：人设/方法论/擅长任务/已启用 Skill；召唤按钮占位（任务阶段开放）；404 与加载失败分流提示

### 9.4 验证记录

| 验证项 | 结果 |
|---|---|
| `uv run pytest` | 164 passed, 24 skipped（新增专家 38 + 回归 3） |
| `uv run ruff check .` | PASS |
| `npm run build` | PASS（gzip 80KB） |
| Playwright 闭环一 | 注册→申请→建专家→建 Skill→校验→发布→绑定（默认关）→启用→发布专家→专家中心出现→详情→下架→消失，全通过 |

对抗式审查（22 代理）修复 13 处：category:null 500→400、skill_count 口径泄漏/不一致、绑定错误优先级、LIKE 通配符转义、绑定下拉只列已发布、P04 错误分流、编辑页加载门禁、绑定失败保留选择、绑定错误顶部可见、创建通知经路由 state 传递、searchText 与 URL 同步、错误独占渲染、page 参数加固。

---

## 10. 变更记录

| 版本 | 时间 | 说明 |
|---|---|---|
| v0.1.0 | 2026-09-01 | 记录脚手架首版完成状态与验证结果 |
| v0.2.0 | 2026-09-02 | 补齐数据库文档定义的 11 个索引，新增 Alembic 索引迁移并扩展迁移测试 |
| v0.3.0 | 2026-09-02 | 阶段 1 用户系统垂直切片：4 个端点 + JWT/bcrypt + 统一错误信封 + P01/P05/NavBar 前端 + 23 个后端用例；修复 User 反向 relationship 缺失 |
| v0.4.0 | 2026-09-02 | 阶段 2 Skill 管理垂直切片：validate_skill 纯文本校验器 + Skill 全生命周期 API（状态机）+ P08 前端（列表/弹窗/ValidateButton）+ 61 个新测试 |
| v0.5.0 | 2026-09-02 | 阶段 3 专家 CRUD/绑定/专家中心：闭环一收口；P03/P04/P06/P07；41 个新测试；审查修复 13 处 |
