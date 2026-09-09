# AgentCraft 脚手架搭建进度记录

**文档版本**：v0.12.4

**记录时间**：2026-09-09

**项目位置**：`C:\Users\ChibaRie\Desktop\AgentCraft\agentcraft`

**状态**：脚手架搭建完成，业务代码已落地，正在推进v2

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

## 10. 阶段 4：任务数据层 + SSE 链路（2026-09-03 完成，EchoEngine 冻结契约）

按 Engineering Spec §6.6/§8.3、DB 设计 §3.5-§3.8/§5.5/§6 实现。用 EchoEngine（Mock 引擎）把 SSE 事件契约固定下来，Pi 真引擎阶段只替换引擎实现，前端与事件 schema 不动。

### 10.1 任务 API（§6.6，47 个测试）

| 端点 | 行为 |
|---|---|
| `GET /api/workspaces` | 浏览授权根内子目录（只列目录，不收内容）；相对路径解析 + 越界/符号链接拒绝（junction 以 resolve 包含性校验兜底，`Path.is_symlink` 在 Windows 不识别 junction）；400 非法/404 不存在 |
| `POST /api/tasks` | 201 `{task_id, conversation_id, status, workdir}`；workdir 仅接受根内相对路径（缺省/空=根），派生存储值 `/workspaces/authorized[/<relative>]` 取自规范常量（与 `ck_tasks_workdir` CHECK 绑定，启动 fail-fast 校验配置一致）；专家必须 published（404）；title=description 前 200 字符；派生路径超 VARCHAR(500) 拒绝 |
| `GET /api/tasks` | 分页信封，仅本人，最新在前 |
| `GET /api/tasks/{id}` | 详情含 files + messages（id 兜底秒级 created_at 次序）；他人 403 / 缺失 404 |
| `POST /api/tasks/{id}/messages` | SSE（text/event-stream）；前置校验失败返回统一 JSON 信封（403/404/409）；created/failed→running 原子流转在 data lock 内完成；帧序 `meta → text_delta×N → message_saved → done`，schema 严格按 §6.6；轮锁持有全程，忙时 429 + `Retry-After: 5`（§6.1/§7.2.1 的阶段 4 进程内替代） |
| `POST /api/tasks/{id}/files` | 前置（status=created 且无用户消息）+ 落库前在 data lock 内复核（manifest 冻结与首条消息互斥）；文件名规则（basename/控制字符/255）+ 单文件 20MB + 单次 10 个 + 任务累计 100MB；流式 SHA-256；失败补偿整批回滚；413 细分 `FILE_TOO_LARGE/FILE_COUNT_EXCEEDED/FILE_QUOTA_EXCEEDED`；mime_type 白名单消毒（仅展示用） |
| `GET /api/tasks/{id}/files` | 元数据列表，`agent_path=/task-files/<uuid>` |

快照冻结（DB 设计 §6）：expert_name/avatar、skill_snapshot（enabled ∩ published 的完整内容 + persona/methodology + loaded_at，段标签「角色/目标/工作步骤/输出要求/约束」）、mcp_snapshot（v1 空 tools）；创建后专家改名/Skill 改内容不影响快照（测试冻结验证）。

启动巡检（lifespan）：确保 workspace/task-files 存储根存在；清理超时 staging（>1h）与无 TaskFile 元数据的孤儿文件（崩溃补偿兜底）；`UploadSizeGuard` 中间件在 multipart 预落盘前按 Content-Length 拒绝超量请求（认证用户磁盘 DoS 防护）。

### 10.2 前端 P09

- `/tasks/new`：专家选择器（`?expert=` 预选，P04 召唤按钮解锁）→ 任务描述 → WorkdirSelector（授权根内钻取，仅提交相对路径）→ 创建后进入对话
- `/tasks(/{id})`：左侧任务列表（状态徽标/点击切换）+ 顶部信息栏（快照专家名/workdir/状态脉冲徽标）+ MessageList 流式渲染（text_delta 逐字追加 + 流式光标动画）+ 附件 chips；附件仅首条消息前可传，发送后按钮禁用
- `api/sse.js`：SseClient（Fetch + ReadableStream + Bearer；EventSource 因仅支持 GET 且无法携带 Authorization 弃用，§8.3）；非 OK 响应解析统一错误信封透出 403/404/409
- 切换任务路由 `key` 强制重挂载 + 组件内 activeTaskIdRef 双保险：旧流 abort、状态全重置（含输入草稿）、过期 refresh 响应丢弃；对账失败保留乐观回复并显式提示；near-bottom 检测后才自动滚底

### 10.3 验证记录

| 验证项 | 结果 |
|---|---|
| `uv run pytest` | 204 passed, 31 skipped（新增任务 47） |
| `uv run ruff check .` | PASS |
| `npm run build` | PASS（gzip 87KB） |
| Playwright 闭环二 | P04 召唤→创建任务（workdir 浏览）→首条消息前上传附件→发消息→EchoEngine 流式回复→多轮→刷新历史可查→发送后附件禁用，全通过 |

对抗式审查（25 代理，21 发现 → 18 确认）修复 15 项：上传配额 TOCTOU + manifest 冻结复核（per-task data lock）、running 并发发送 429（per-task round lock，seq 锁内计算）、multipart 预落盘 DoS 守卫、Windows junction 逃逸、mime_type 消毒、workdir 别名/超长拒绝、AGENTCRAFT_WORKSPACE_ROOT 与 CHECK 一致性 fail-fast、快照「工作步骤」标签对齐、前端切任务旧 SSE 流污染（CRITICAL）、refresh 乱序守卫、对账失败乐观保留、草稿重置、near-bottom 滚动、死代码清理。3 项被怀疑者否决（并发上传 SQLite 锁误报等）。

遗留接缝：complete/abort/delete 与 MCP 绑定端点仍 501；EchoEngine→PiEngineManager（§7.2）替换时轮锁升级为跨进程 mutation lock；description≤2000 / content≤32000 为规格未记载的实现上限（待规格补记）。

---

## 11. 阶段 5：Pi 引擎集成（2026-09-03 完成，faux 全链路验收）

按 `docs/phase5_piagent.md` 执行手册实现 §7 核心链路。事实源对齐 pi 0.84.3 源码（rpc.md + faux.ts + 扩展加载器），步骤 1 手工 JSONL 实验录制真实帧序存 `tests/fixtures/pi_frames/`（faux_basic/faux_abort），作为协议测试语料。

### 11.1 交付内容（4 文件 + 引擎替换）

| 模块 | 要点 |
|---|---|
| `engine/pi_engine.py` | 协议封装：JSONL LF 分帧（剥离尾部 \r）、pending {id: Future} 自增关联、handle_line 三分叉（response→Future；extension_ui_request→confirm=false/select·input·editor=cancelled 2s 自动应答；事件→回调）、writer lock 串行 stdin、stdout 独立排空协程（1MiB 行长上限内存防线）、id 错配→needs_rebuild、坏行跳过 |
| `engine/event_handler.py` | Pi 事件→SSE 翻译（§6.6 冻结 schema）：text_delta/thinking_delta 转发、tool_execution_* → tool_event + role=tool 落库、message_end 仅 stopReason=stop 落库（aborted/error 丢弃，防重播种喂回残缺上下文）、agent_settled → done、usage 映射 prompt/completion_tokens |
| `engine/skill_loader.py` | §7.5 组装：专家身份→人设→方法论→Skill（任务级 nonce 边界包裹 + 同形子串剥离）→TaskFile manifest（独立 nonce + 文件名是数据声明）→工作规则（项目上下文非更高优先级声明）→末尾忽略声明→cwd；64KiB UTF-8 上限，创建时 413（PROMPT_TOO_LARGE） |
| `engine/pi_engine_manager.py` | 容器表 + ensure_container 惰性创建/重建（needs_rebuild/引擎死亡）；**重播种**（§7.6）：新容器首条消息嵌入最近 40 条历史（`[历史对话回顾]`+`[当前消息]`，绝不单独发历史防幻影轮）；run_round 整轮 deadline 超时兜底 abort；request_abort 绕 mutation lock；有界轮队列（增量帧可丢弃、关键事件必达）；任务令牌随容器轮换 |
| `engine/docker_transport.py` | `ContainerSpec` 单一事实源（CLI/API 双通道防漂移）：argv 数组直传、三挂载、非 root、只读 rootfs、cap_drop ALL、no-new-privileges、tmpfs（/tmp + ~/.pi 凭证临时区）、internal 网络、512MB/1CPU、labels；DockerApiTransport（aiodocker）+ DockerCliTransport（`docker run -i` stdio，npipe 环境回退）+ stderr 诊断日志 |
| `engine/extension_generator.py` | §7.4 生成 task.ts：MCP 工具注册循环（mcp_snapshot 写死）+ faux provider 注册块（AGENTCRAFT_PROVIDER=faux 启用） |
| `engine/subprocess_transport.py` | 本地子进程传输（PI_RUNTIME=subprocess，Windows 开发直跑，无沙箱仅开发用） |

**引擎替换**：`POST /api/tasks/{id}/messages` 改经 PiEngineManager.run_round（EchoEngine 删除）；`POST /api/tasks/{id}/abort` 落地（202，所有权检查后绕锁 abort）；PI_RUNTIME=auto 时 docker API→docker CLI→本地子进程依序回退。

### 11.2 规格修正（以源码为准，待规格升版补记）

1. **CLI 无内置 faux**（实测 `Unknown provider "faux"`）：经任务扩展 `pi.registerProvider` + 自定义 streamSimple 回显注册——回显含上下文尾部，「回复引用第 1 轮事实」即连续性的确定性证据；AGENTCRAFT_FAUX_CHUNK_DELAY_MS 控制分帧节奏（abort 可观察性）
2. **abort 帧序**：abort 后 pi 仍发 assistant `message_end`（stopReason=aborted）+ `agent_settled` 照常收尾——落库纪律必须过滤 stopReason
3. **只读 rootfs 需要 ~/.pi tmpfs**：pi 凭证存储在 HOME 下建目录，只读 rootfs 下 ENOENT；tmpfs 随容器销毁
4. **--mount 语法**：只读为 `readonly` 标志（非 `ro` 后缀）；bind source 必须绝对路径（相对路径 docker CLI 直接报 invalid Windows path）
5. **轮超时以整轮 deadline 计**（防慢流逐段重置超时）；user 消息也有 message_start/end 对（落库按 role 过滤）

### 11.3 验收（PRD §4.5.5，faux + Docker 容器运行时，acceptance_pi_e2e.py）

- ✅ faux 创建任务→发消息→P09 逐字流式（非首条 0.03s，首条 <10s）
- ✅ 连续多轮，回复引用第 1 轮暗号（内存连续性）
- ✅ `docker rm -f pi-task-N` 后发消息→自动重建+重播种，回复含 `[历史对话回顾]` 中的第 1 轮事实
- ✅ abort：202→done(aborted)→半截回复不落库→任务保持 running 可继续
- ✅ `docker inspect`：仅三个规定挂载、agentcraft-internal 网络、只读 rootfs、cap_drop ALL、非 root、env 无真实 Key（仅任务令牌）
- ✅ 全部消息落库（4 user + 3 assistant，无中止残片）
- ✅ `tests/test_pi_engine.py`（协议 13）+ `test_pi_manager.py`（重播种/abort/令牌 9）+ `test_event_handler.py`（翻译 8）全绿；全量 pytest 通过、ruff 零告警、vite build 86.5KB

pi-worker 镜像 `agentcraft-pi-worker:0.84.3`（node:22-slim，非 root piworker）；受限网络经 `--build-arg NPM_REGISTRY=https://registry.npmmirror.com` 构建。

### 11.4 遗留接缝（阶段 6/7）

- `/internal/mcp/call` 后端实现与 MCP 工具真实验证（阶段 6）：任务令牌与扩展 registerTool 模板已就位
- mutation lock 跨进程化、并发上限排队、空闲回收、崩溃恢复重试 3 次、Skill 指纹 kill switch、看门狗巡检（阶段 7）
- provider-proxy（阶段 6）：faux→openai 切换时 OPENAI_BASE_URL/KEY 注入路径已预留
- complete/delete 端点（阶段 7 complete 语义：无锁预检→request_abort→等锁→completed+回收容器）

---

## 12. 阶段 5.5：Provider 双模式 BYOK（2026-09-03 完成，真实流量打通归阶段 6 proxy）

按修订后的 PRD/DB/手册基线（BYOK 提入 P0）实现用户自带 Provider：

- **加密信封**：`utils/crypto.py` AES-256-GCM（§11.3 同款方案换 AAD=`agentcraft:user_providers:{user_id}:api_key:v1`）；密钥环复用 `MCP_ENCRYPTION_*`，ACTIVE_KID 显式校验；写时派生 `api_key_hint`（尾 4 位）避免读路径解密
- **DB**：`user_providers` 表（用户内名称唯一 / protocol CHECK / is_default）+ `tasks.provider_config_id`(SET NULL)/`provider_snapshot`（含 Key 信封密文，快照自足——删除配置不阻塞既有任务）；迁移用 batch 模式（SQLite 不支持 ALTER 加 FK）
- **API**：`/api/providers` CRUD——Key 仅写入（响应只有掩码）、三态更新（缺席/清 null/换值）、is_default 用户内互斥、所有权统一 404（消除存在性预言）、密钥环未配置 503 `ENCRYPTION_UNCONFIGURED`；base_url URL 解析校验（scheme/主机名/禁内嵌凭据；回环不封禁——本地 Ollama 合法，出口防线在 proxy）
- **任务侧**：`POST /api/tasks` 可选 `provider_config_id`，回退链（显式→用户默认→系统 `.env`）→ `provider_snapshot` 冻结；详情返回 `provider` 摘要（`api_key_set` 布尔，无任何 Key 形态）
- **指纹重建**：`ensure_container()` 当前生效配置（新鲜解析）vs 容器启动指纹，不一致 → teardown+重建+重播种；DB 快照保持冻结（历史事实源）；容器 argv 按解析值的 protocol/model_id
- **前端 P10**：`/settings/providers` 列表（默认徽标/掩码）+ 表单（三态 Key 语义）+ 删除确认 + NavBar 入口；TaskCreatePage Provider 选择器（默认项=用户默认/系统）

边界：**容器 env 的 OPENAI_BASE_URL 仍指向 provider-proxy**——非 faux 用户 Provider 的真实流量在阶段 6 proxy 按令牌路由 + Responses→Completions 兼容转换后打通（faux 链路与全部数据/快照/指纹行为已于本阶段完整验收）。faux 为内置测试项不经 user_providers 表。

### 12.1 Proxy 提前落地 + 真实模型验收（2026-09-03，用户实测通过）

用户实测 BYOK 对话报 `Connection error.`（容器指向的 provider-proxy 不存在）——按既定架构决策把 proxy 核心提前实现并验收：

- **JWT 任务令牌**（`services/task_token.py`）：TASK_TOKEN_SECRET 签名（V2 Phase 0 Task 1 起与 SECRET_KEY 分离），claims={task_id, instance, model, exp=24h}；instance 随容器启动轮换；proxy 无状态校验（撤销列表留阶段 7，本地单操作者可接受）
- **provider-proxy**（`backend/provider_proxy.py`，容器 `agentcraft-provider-proxy`）：`/v1/chat/completions` 认证→model scope→查任务快照→解密 Key→路由上游，SSE 流式中继/JSON 缓冲透传，上游错误原样透传、不可达 502；`/v1/models` 返回快照模型；免钥上游（Ollama）不发认证头；系统默认模式走 `PROVIDER_PROXY_UPSTREAM`+`OPENAI_API_KEY`
- **协议兼容（实测确认）**：Pi 内置 openai provider 使用 **Responses API**（`openai.ts` 引 `openAIResponsesApi`），DeepSeek/Ollama 等普遍未实现——任务扩展对非 faux 注册 `api: "openai-completions"` 的 openai provider 覆盖，Pi 改说 chat/completions，**无需转换层**
- **容器化**：proxy 容器双网络（bridge 出公网 + agentcraft-internal 被 Pi 以 `provider-proxy:8080` 访问；实测 internal 网络对 host-gateway 为 ENETUNREACH，故 proxy 必须容器化）；代码与 .env 只读挂载；`manager.ensure_proxy()` 自动保障存活
- **验收（用户实测）**：绑定 DeepSeek（deepseek-v4-flash）→ 建任务 → 对话全链路通过；proxy 无令牌 401、出站可达上游均验证；容器内 11 个 proxy 测试 + 5 个令牌测试全绿
- 网络实测记录：Docker Desktop 自定义 internal 网络不解析 `host.docker.internal`（EAI_AGAIN）、host-gateway 不可达（ENETUNREACH）——「proxy 必须同网络容器化」的依据

### 12.2 阶段 6：MCP 管理 + MCP 桥（2026-09-03 完成，闭环三收口）

PRD §3.3 闭环三全链路落地：注册 Server → 发现工具 → 启用/敏感授权 → 发布 → 绑定专家 → 任务快照冻结 → Agent 对话中真实调用 → kill switch 即时阻断。

- **MCP 客户端**（`engine/mcp_client.py`）：MCP 2024-11-05 JSON-RPC 2.0；stdio 传输（LF 分帧）+ streamable HTTP 传输（Mcp-Session-Id 保持，json/SSE 应答兼容）；`resolve_stdio_argv` 有 docker → `docker run --rm -i --network internal` mcp-sandbox 沙箱（env 经 -e 注入、命令经 sh -c），无 docker → 本地子进程开发回退；单请求超时 30s、结果 >100KB 截断（`MCP_RESULT_MAX_BYTES`）；env 密钥不落日志
- **管理服务**（`services/mcp_service.py`）：Server CRUD（transport 条件必填）+ discover 落库（新工具默认 sensitive=1/enabled=0，仅可信只读 allowlist 置非敏感；重复 discover 更新描述/schema 并保留状态）+ 工具开关（sensitive 启用需 confirm→写 `authorized_at`，禁用即清空）+ publish（需≥1 工具）/offline + 删除（绑定或 `mcp_snapshot` 引用 409，可先下架止损）+ 专家绑定三操作（owner 归属 404 化、Server 必须 published、UNIQUE 409）
- **env 加密信封**：§11.3 同款 AES-256-GCM，AAD=`agentcraft:mcp_servers:{server_id}:env_vars:v1`；API 只回变量名，运行期仅 MCPService 内存解密
- **快照装配**：`load_snapshot_tools`（enabled 绑定 ∩ published Server ∩ enabled 工具 ∩ 非敏感或已授权）→ `tasks.mcp_snapshot` 冻结；扩展 `task.ts` 按快照生成 `pi.registerTool` + fetch 回调（§7.4 模板照抄）
- **/internal/mcp/call**（§6.8）：X-Task-Token 三重校验（签名/任务一致/实例一致——旧容器令牌失效）→ 快照能力上限（404）→ kill switch 双层（Server published/工具 enabled/绑定 enabled/敏感授权非空且不早于快照时点 → 403）→ 执行（502 上游失败）；结果 {content, is_error} 透传
- **mcp-sandbox 沙箱**（`docker/Dockerfile.mcp-sandbox` + compose profile）：node22-slim + `@modelcontextprotocol/server-filesystem` 预装、非 root、无挂载、internal 网络；真实探针 `probe_mcp_stdio.py` 实测 14 工具发现 + tools/call 全通
- **dev 后端转发器**（`docker_ensure_backend_forwarder`）：internal 网络无 host 路由（阶段 5 实测），容器回调 /internal/mcp/call 需要「agentcraft-control」在网内可达——双网络转发容器（pi-worker 镜像 node 脚本，internal 别名 agentcraft-control + bridge 转发 host:8000），与 provider-proxy 同款模式；compose 形态（控制面已容器化）自动跳过
- **前端**：P08 `/skills` 激活 MCP Server 标签页（注册表单含 env 键值对/发现/工具开关+敏感确认条/发布/下架/删除确认）；P07 专家编辑 MCP 绑定面板（绑定/启用/解绑，不展示连接信息）；P09 上下文面板 MCP tab 渲染冻结快照（含敏感徽标）；顺带修复 ExpertEditPage 绑定成功路径 `setSelectedSkillId` 越界引用的潜在 ReferenceError
- **E2E 实测发现并修复**：Pi 在模型发起工具调用时以 `stopReason="toolUse"` 结束 assistant 消息——属工具循环正常中间步，EventHandler 原把非 stop 全部当 error 帧下发（faux 无工具未暴露）；已改为静默忽略并补回归测试；另修复 aiodocker 探测失败的 Unclosed client session 泄漏

### 12.3 验收记录（阶段 6，2026-09-03）

- 单测基线：337 passed / 49 skipped（新增 MCP 客户端 12、服务层 14、API 15、内部调用 12、toolUse 回归 1 等）；ruff 全绿
- 真实 MCP Server：`probe_mcp_stdio.py` 经沙箱容器 discover 14 工具 + list_directory 全通
- UI 全流程（浏览器，chibarie 账号，0 console error）：P08 注册「文件系统」（stdio）→ 发现 14 工具（只读 allowlist 无敏感标、写类带敏感标）→ 启用 list_directory → 敏感工具确认条验证 → 发布 → P07 绑定专家并启用 → 建任务（快照冻结「1 个 MCP 工具」）→ P09 MCP tab 渲染
- 闭环三 E2E（真实模型 deepseek-v4-flash BYOK）：①Agent 对话中真实调用 list_directory（沙箱内 /workspace）+ 内置 bash 补充核实，回复含真实目录内容并落库；②P08 禁用 list_directory → 既有任务下一条消息再次调用 → **HTTP 403 立即阻断**（错误结果注入上下文，Agent 自行降级用 ls 核实并如实披露）；③重新启用 → 调用恢复，SSE 流 0 error 帧
- 已知边界：mcp-sandbox 为共享镜像无任务挂载——MCP 文件系统 Server 看到的是沙箱自身 /workspace（空），与任务 workdir 不同（Pi 内置 bash/read/write 才操作任务工作区）；按任务挂载留阶段 7+ 权衡

### 12.4 阶段 7：任务生命周期完善（2026-09-03 完成）

- **生命周期端点（§7.2.1 锁语义表逐行）**：`task_lifecycle.py`——complete（无锁预检活动轮→request_abort 绕锁→等待并持有 mutation lock→持锁复验 running→completed+回收容器+失效令牌；created/failed/completed 409）；delete（round→data 锁序与发送一致防死锁；先停容器再级联删会话/消息/文件元数据/上传目录/扩展文件，项目目录不动）；abort 细化（仅 running 且存在活动轮可中止，否则 409 `TASK_NO_ACTIVE_ROUND`，202 不改终态）；专家下架联动（§7.8：该专家 running 任务原子置 completed+回收容器）。顺带修复 Task.conversation relationship 缺 cascade 导致 ORM 删除报 NOT NULL 的问题
- **并发上限（§7.2/§6.6）**：容器槽位信号量（`PI_MAX_CONCURRENT_CONTAINERS`，holder 集合一一配对释放）；槽满时新发送先收到 SSE `queued` 再排队，槽释放自动继续
- **空闲回收（§7.2）**：每任务最近活动时刻（loop.monotonic）；后台巡检发现连续 `PI_IDLE_TIMEOUT_MINUTES` 无活动且无活动轮→回收；下条消息自动重建+重播种
- **看门狗与总超时（§7.8.1）**：后台循环（30s）双巡检——容器死亡（轮间）→回收+failed（可重试）；running 累计时长超 `PI_TASK_MAX_LIFETIME_MINUTES`（默认 30）→abort+failed+回收。DB 触点经 dependencies 注入 fetcher/marker（仅 running→failed，不覆盖 completed）
- **崩溃恢复（§7.8）**：run_round 轮中同时等待事件流与容器存活（reader EOF 即崩溃信号，不空等超时）→teardown+重建+重播种重发，最多 3 次；耗尽→failed+`ENGINE_CRASHED` error 帧
- **check_code_style（§6.8/§7.4）**：`harness_service`——/workspace 相对路径解析（拒绝绝对/`..`/解析逃逸/缺失）；ruff format --check + check（30s 超时，rc>=2→502；ruff 定位 PATH→venv）；issue 解析（format/check 两种行格式）；扩展模板固定追加注册块；端点复用 X-Task-Token 三重校验。内部接口全部令牌门禁（不留未鉴权 501 面）
- **前端**：P05 个人中心「我的任务」列表（专家名/标题/状态/时间，仅本人）；P09 头部生命周期按钮（中止=活动轮可见、结束对话=running 可见+confirm、删除=confirm→级联→跳列表）、failed 重试提示、completed 输入框锁定、queued 排队提示；顺带修复 /tasks 列表页 `GET /api/tasks/null` 的无守卫请求
- **commit 安全审查处置**：http-sse URL 校验（scheme/主机名/禁凭据；私网立场与 BYOK 一致并注释）；env 变量名格式校验+stdio 命令沙箱语义注释；上游错误文本不回显前端（详情仅日志）

### 12.5 验收记录（阶段 7，2026-09-03）

- 单测基线：368 passed / 52 skipped（新增生命周期 API 12、引擎生命周期 9、harness 12）；ruff 全绿
- 看门狗实测：后端启动后 30s 巡检将两个今晨遗留 running 任务（总时长>30min）标记 failed 并回收容器（日志：「任务总超时，abort + failed + 回收容器」）——failed 任务经重发消息重试恢复 running 实测通过
- PRD §4.5.7 逐条（浏览器 0 console error）：①创建即建会话✓ ②上传隔离+非所有者拒绝✓（阶段 4 契约） ③首条消息后禁传✓ ④目录上传阻断✓ ⑤超限无残留✓ ⑥流式+双方落库✓（真实模型） ⑦**中止实测**：流中点中止→半截回复未落库、任务保持 running✓ ⑧列表仅本人✓（非本人 403） ⑨详情时间序✓ ⑩专家下架阻断✓（联动 completed） ⑪**删除级联实测**：确认条→会话/消息/文件/任务行全删（DB 复核 0 行）→跳回列表✓；**complete 实测**：running→结束对话→已结束→输入框锁定✓

---

## 13. 变更记录

| 版本 | 时间 | 说明 |
|---|---|---|
| v0.1.0 | 2026-09-01 | 记录脚手架首版完成状态与验证结果 |
| v0.2.0 | 2026-09-02 | 补齐数据库文档定义的 11 个索引，新增 Alembic 索引迁移并扩展迁移测试 |
| v0.3.0 | 2026-09-02 | 阶段 1 用户系统垂直切片：4 个端点 + JWT/bcrypt + 统一错误信封 + P01/P05/NavBar 前端 + 23 个后端用例；修复 User 反向 relationship 缺失 |
| v0.4.0 | 2026-09-02 | 阶段 2 Skill 管理垂直切片：validate_skill 纯文本校验器 + Skill 全生命周期 API（状态机）+ P08 前端（列表/弹窗/ValidateButton）+ 61 个新测试 |
| v0.5.0 | 2026-09-02 | 阶段 3 专家 CRUD/绑定/专家中心：闭环一收口；P03/P04/P06/P07；41 个新测试；审查修复 13 处 |
| v0.6.0 | 2026-09-03 | 阶段 4 任务数据层 + SSE 链路（EchoEngine 冻结契约）：任务/文件/工作区 API + P09 前端 + 启动巡检/体量守卫/任务锁；47 个新测试；审查修复 15 项 |
| v0.12.4 | 2026-09-09 | BYOK 模型图像输入声明修复：多模态模型（GLM-5.3-Flash）read 图片后图像块被 Pi 剥离，模型自称"不支持图像输入"（Pi read.js `getNonVisionImageNote` 按 `model.input` 是否含 "image" 判定，输出固定话术 `[Current model does not support images...]`）。根因：extension_generator `_NO_FAUX_BLOCK` 硬编码 `input: ["text"]`，所有 BYOK 模型被注册为纯文本。修复：真实 Provider 注册块统一声明 `input: ["text", "image"]`（faux 回显引擎保持纯文本）；副作用明示——纯文本模型读图将收到 provider 侧可观察错误（不崩溃）；V2 BYOK 目录化（阶段3）改为按模型能力如实声明。回归：tests/test_harness.py `test_extension_declares_image_input_for_real_provider`（TDD RED→GREEN）；用户实测图像描述正常 |
| v0.12.3 | 2026-09-08 | Pi 传输层大行帧崩溃修复：任务4 Agent read 855KB 图片后，Pi 以 ≈1.17MB 单行 JSONL 回传 tool_result，asyncio 默认 64KB 行上限在 DockerCliTransport.readline（docker_transport.py）先抛 ValueError，行未及引擎 MAX_LINE_BYTES 防护即崩溃，恢复 3 次同因耗尽任务 failed。修复：MAX_LINE_BYTES=32MiB（20MiB 上传上限×4/3 推导）定为共享常量；CLI/subprocess 双传输 create_subprocess_exec 传入 limit 且超限帧排空丢弃返回空行哨兵（handle_line 静默跳过，轮次正常收尾）；DockerApiTransport 补 _LineAssembler demux chunk 行还原器 + 同款超限丢弃与 2×硬顶，完成三传输大帧语义对齐；close() 补进程收割消除测试拆解噪声；EventHandler 仅提取 text 块，图像 base64 本不入库不进 SSE（§6.6 契约不变）；回归 tests/test_transport_line_limit.py 12 用例（生产同源 RED 先行确认），全量 409 passed + 52 skipped |
| v0.12.2 | 2026-09-05 | 任务对话附件补传（§6.6 放宽）：上传门禁「首条消息前」→「任务活跃期 created/running/failed」（终态仍拒，TaskAlreadyStartedError 废弃→TaskStateError 409）；引擎 `seeded_file_ids` 播种集合 + 出站消息 `[附件更新]` 块（新文件只告知一次，重建路径 manifest 全量收敛）；P09 canAttach 放开 + tooltip；Spec 6 处契约同步；新增/更新测试 5 项 |
| v0.12.1 | 2026-09-03 | 体验打磨与 Skill 导入：P02 后导航补「Skill / MCP 管理」入口；专家中心页头新增「新建专家/我的专家」入口（expert 角色）；P08 页头随 tab 联动；MCP 工具行重排（名/简介两行对齐）；任务对话页入场动画补齐（侧栏/页头/输入区/上下文面板 rise + 既有消息动画）；**Skill 导入**（.md → Provider LLM 拆解填栏 / .zip → 安全解包落盘 skill-packages/skill-{id}/ + 文件树入提示词，产物 draft，测试 11 项）；移除敏感工具多次确认（开关直接生效，启用即记录授权时点）；移除 composer 底部固定文案；基线 393/52 |
| v0.12.0 | 2026-09-03 | 收尾：沙箱隔离性测试补齐（15 项，双通道清单不漂移）、P02 首页（仿智能体中心信息架构：问候+速览墨面+最近任务+精选专家）、日间/夜间主题切换（右上角+系统回退+无闪烁）、tools/seed_demo.py 幂等演示种子、README 重写；基线 382/52 |
| v0.11.0 | 2026-09-03 | 阶段 7 任务生命周期：complete/delete/abort（§7.2.1 锁语义表）/并发上限 queued/空闲回收/看门狗+总超时/崩溃恢复/check_code_style/P05 任务列表/P09 生命周期按钮；看门狗与中止/complete/删除级联实测通过；基线 368/52 |
| v0.10.0 | 2026-09-03 | 阶段 6 MCP 管理 + MCP 桥：MCP 客户端（stdio/http）/mcp_service（env 信封/敏感授权/快照装配）/API 全套/内部调用端点/mcp-sandbox 沙箱/dev 转发器/P08+P07+P09 前端；闭环三 E2E（真实模型+真实 Server，kill switch 即时阻断）；toolUse error 帧误报修复；基线 337/49 |
| v0.9.0 | 2026-09-03 | Proxy 提前落地 + 真实模型验收：JWT 任务令牌/provider-proxy 按令牌路由/completions 协议扩展/双网络容器化；DeepSeek BYOK 真实对话用户实测通过 |
| v0.8.0 | 2026-09-03 | 阶段 5.5 Provider 双模式 BYOK：加密信封/user_providers 表/CRUD/任务快照冻结/Provider 指纹重建/P10 设置页；测试基线 274/36；安全审查修复（ACTIVE_KID 校验、所有权 404 化、SSRF 立场注释） |
| v0.7.0 | 2026-09-03 | 阶段 5 Pi 引擎集成：PiEngine 协议层 + SkillLoader + EventHandler + PiEngineManager（重播种/abort/容器池）+ Docker CLI/API 双传输 + faux 经扩展注册；EchoEngine 替换、abort 落地；faux 全链路 E2E 验收（含 docker rm -f 重播种恢复与沙箱 inspect 清单） |
