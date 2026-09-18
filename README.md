# AgentCraft：多租户 AI 专家工作台

[![API](https://img.shields.io/badge/API-2.0.0-blue)](https://github.com/ChibaRie/AgentCraft)
[![Coverage](https://img.shields.io/badge/coverage-88%25-brightgreen)](https://github.com/ChibaRie/AgentCraft)

把领域经验封装为可复用的「专家」——人设 + 方法论 + Skill（能力包）+ 平台工具，
审核发布后供受邀用户召唤。每个任务运行在**独立 Docker 沙箱容器**里的 Pi 编码代理中，
模型请求经**按任务令牌路由的薄代理**转发到用户自己配置的 Provider（BYOK），
真实 Key 永不进入容器。

**当前形态：V2 Public Beta 单轨**——V1 应用面已物理删除，契约路径统一为 `/api/*`
（admin 独立 `/api/admin`），会话单轨 `v2Session`。API 版本 **2.0.0**。

> 技术栈：FastAPI + SQLAlchemy 2.0 + PostgreSQL 16（RLS 双库角色）+ Alembic ｜
> React 18 + Vite ｜ [Pi coding-agent](https://github.com/earendil-works/pi-coding-agent)@0.84.3 ｜ Docker

## 项目定位：三个工程问题

| 问题 | 做法 | 关键机制 |
|---|---|---|
| **模型密钥合规**：平台不应持有、任务不应接触用户 Key | 用户自带 Provider（BYOK），平台只存信封密文 | AES-256-GCM DEK/KEK 信封入库；真实 Key 只在 provider-proxy 进程内按任务 grant 短暂解封；任务容器只拿到代理地址与任务级令牌 |
| **运行环境隔离**：Agent 会读写文件、执行代码 | 每个任务一个一次性沙箱容器 | 只读 rootfs、非 root、`cap_drop ALL`、仅 internal 网络、**零业务挂载**（文件收发全部走控制面回调） |
| **工具越权**：Agent 可能越权使用平台能力 | 能力清单在创建任务时冻结，执行时双重校验 | `revision_tools` 为能力上限 + 回调复核 `tool_catalog` kill switch + `round lease_epoch` 围栏 |

## 功能特性

### 用户面

- **邀请制账户**：管理员发放与邮箱绑定的一次性邀请（≤7 天）→ 邮箱验证 → 激活；
  Argon2id 口令、不透明会话 cookie `ac_session`（HttpOnly/Secure/SameSite=Strict）+
  同源 `X-CSRF-Token`；TOTP 可选；密码重置；设备会话列表与单会话撤销；
  注销有 14 天撤销期（邮件一次性取消 token 可恢复）
- **BYOK Provider**：自带 https 上游地址（OpenAI 兼容）+ 自定义模型名 + Key；
  Key 信封加密入库、接口只回尾 4 位掩码；提供连通性测试；
  未配置有效 Provider 时不能创建任务
- **用户自带 MCP**：注册 stdio/http MCP Server（stdio 的命令+参数+env 整体信封
  加密，API 零明文出参）→ 发现工具并缓存 → 建任务时挂载（≤3 个，快照冻结）；
  stdio 命令只在一次性 `mcp-sandbox` 容器内执行，工具调用经 `/internal/mcp/call`
  治理链（任务令牌 + epoch fence + 快照能力上限 + server/tool 启停门）；
  server 停用/删除联动终止挂载它的 queued/running 任务
- **专家发现与任务流**：浏览/搜索公开专家 → 四步建任务（选专家 / 选 Provider /
  写任务描述 / 上传输入文件）→ 显式「开始任务」冻结输入清单与 SHA-256 →
  排队或运行 → 流式对话 → 下载产物（AI 生成标识随元数据返回）
- **流式对话与对账**：SSE 事件协议（`meta`/`queued`/`text_delta`/`tool_event`/
  `message_saved`/`status_changed`/`done`/`error`）；断连后按 `?after=<sequence>`
  重连补齐；消息与状态以 `task_events` 为唯一事实源，delta 仅作瞬态渲染

### 作者面（`expert_author` 授权）

- 专家 / Skill 草稿 → 提审 → 审核通过后原子发布；发布内容是**不可变 revision** +
  规范化内容 SHA-256
- 编辑已发布内容会生成新 revision，旧公开版本保持可用，直到新版本过审并原子切换
- 支持下架（offline）与删除，操作与审计同事务

### 管理面（`/api/admin`，管理员 TOTP 三重门）

- 邀请管理、用户管理（封禁级联：撤销会话、终止任务、释放配额）、专家与 Skill 审核、
  举报处置与下架、目录与工具 kill switch、审计查询、admin 产物下载（先审计后读）

### 任务运行时

- **detached 轮执行**：HTTP 请求只落库并返回 202，后台 dispatcher 领取平台槽位、
  executor 执行轮；`round lease` + `lease_epoch` fencing 防双执行，
  lease 过期先围栏、确认容器已死后再重试（≤3 次）
- **原子配额**（PRD §4.1 默认值，管理员可下调）：用户 1 个运行 / 3 个存活任务、
  每日 5 个新任务；平台 2 个运行槽位；输入+产物 10 MiB/任务、用户 1 GiB、
  平台 60 GiB——全部走 reservation 行 + 条件更新，禁止「先 COUNT 再插入」
- **文件安全**：输入文件仅 `uploading` 状态可增删，提交后任何变更被拒；
  文件名单路径段校验；产物经控制面回调登记并复制到只读下载区；终态任务在线保留 7 天
- **平台工具**（5 个，全部经 `/internal/tools/*` 回调执行，容器零数据挂载）：
  `check_code_style`、`read_task_file`、`write_output_file`、`list_task_files`、
  `query_task_state`；工具被停用/下架即时阻断既有任务（kill switch）
- **运行边界**：轮级 deadline（默认 20 分钟）、`uploading` 24 小时未提交输入自动失败、
  并发上限触发 SSE `queued` 排队、超时与崩溃由 reclaim 作业收口
- **主题**：日间/夜间一键切换，跟随系统默认，无闪烁

## 架构总览

```
浏览器 ──HTTP/SSE（/api/*，cookie 会话 v2Session）──> agentcraft-control（FastAPI）
                        │  PostgreSQL 16（RLS 双角色：agentcraft_app / agentcraft_admin）
                        │  outbox 派发 / 删除清扫 / 任务 dispatcher + executor 后台循环
                        │
                        ├─ 每任务 docker run ──> pi-task-<id>
                        │      只读 rootfs、非 root、cap_drop ALL、仅 internal 网络
                        │      仅挂载扩展脚本（全回调模型：无工作区/文件挂载）
                        │      ├─ 模型请求 ──> provider-proxy/v1（Bearer 任务令牌）
                        │      │        └─ grant 兑换 ──> /internal/provider-grant（专用凭据）
                        │      └─ 平台工具 ──> /internal/tools（X-Task-Token + 围栏 + kill-switch）
                        └─ 后台：dispatcher 领槽 / executor 执行轮 / reclaim / 对账
```

安全边界要点：任务容器仅入 internal 网络且**零业务挂载**；平台工具能力以
`revision_tools` 为上限，回调凭任务级 `X-Task-Token` 并叠加 `lease_epoch` 围栏与
`tool_catalog` kill-switch 二次校验；真实 Provider Key 只存在于 provider-proxy
进程内存（控制面按轮签发 grant，settle 即作废）。多租户隔离由 PostgreSQL RLS
（`agentcraft_app` / `agentcraft_admin` 双角色、不可互相 `SET ROLE`）与统一 404 承载。

## 快速开始

前置：Python 3.12+、Node 22+、PostgreSQL 16、Docker Desktop（沙箱需要）。

```bash
# 1) 依赖
uv sync
cd frontend && npm install && cd ..

# 2) 配置（cp .env.example .env 后至少填这些）
#    SECRET_KEY / TASK_TOKEN_SECRET / PROXY_GRANT_SECRET
#    V2_DATABASE_URL + V2_ADMIN_DATABASE_URL（app / admin 双角色；两者同非空才进 V2 模式）
#    MFA_ENCRYPTION_KEY / EMAIL_OUTBOX_ENCRYPTION_KEY / RATE_LIMIT_HMAC_KEY /
#    PROVIDER_KEY_ENCRYPTION_KEY（四把 b64url 32B 独立密钥）

# 3) 迁移（V2 主链；V1 链见下「数据库迁移双链」）
alembic -c alembic_v2.ini upgrade head

# 4) 启动控制面 + 前端
uvicorn backend.main:app --host 127.0.0.1 --port 8000
cd frontend && npm run dev   # http://127.0.0.1:5173
```

沙箱镜像（首次构建）：

```bash
docker compose -f docker/docker-compose.yml --profile pi-worker build pi-worker
# 用户自带 MCP（stdio）沙箱镜像（Phase 10 M5）
docker compose -f docker/docker-compose.yml --profile mcp-sandbox build mcp-sandbox
```

演示数据（可选，幂等，仅限本地开发）：

```bash
# admin 引导（superuser DSN；首次登录完成 TOTP 注册后过 admin 三重门）
V2_DATABASE_URL=postgresql+asyncpg://<superuser>:<pw>@localhost:5432/<db> \
  uv run python tools/seed_v2_admin.py admin@example.com --password '<初始密码>'

# V2 演示种子：admin → 邀请 → demo 用户激活 → faux Provider → published
# expert/skill → 示例任务（uploading+commit）。须显式 bypass flag——
# published 段直插绕审核，非 localhost DSN 拒绝执行该段；
# 邀请 token 明文仅打印一次，demo 用户经 API 激活后重跑补建示例任务。
uv run python tools/seed_v2_demo.py --i-know-demo-bypasses-review
```

## 测试与质量基线

```bash
# 后端（PostgreSQL 容器或 AGENTCRAFT_TEST_PG_URL 外部库；含覆盖率门槛 80%）
pytest -q --tb=short --cov=backend --cov-report=term-missing --cov-fail-under=80
ruff check backend tests && ruff format --check backend tests

# 前端
cd frontend && npm test && npm run build
```

当前基线：后端 `pytest` 收集 1058 项、全量 0 failed（EXIT=0）；
`backend/` 语句覆盖率门槛 **80%**（CI 强制）；ruff check + format 双绿；前端 vitest
**44 文件 / 429 用例**全绿 + 生产构建通过。CI（GitHub Actions）跑后端 + 前端双 job，
并含 `alembic -c alembic_v2.ini check` 迁移漂移门（同一门已前移为常规测试
`tests/test_v2_migrations.py::test_model_metadata_matches_migrated_schema`）。
覆盖率排除清单见 `pyproject.toml [tool.coverage.run] omit`
（V1 历史迁移链、`tools/`、V1 冻结面）。

## 目录结构

```
backend/
  api/v2/         # V2 契约路由（auth/account/providers/authoring/reports/discover/tasks）
  api/v2/admin/   # admin 七面（invitations/users/reviews/reports/catalog/audit/tasks）
  v2/             # V2 服务层：口令·会话·邀请·outbox·限流·幂等·Provider·审核·举报·
                  #            任务域·调度与执行·工具治理·审计·用户 MCP（mcp_service /
                  #            mcp_sandbox 一次性容器执行链）
  v2/models/      # V2 ORM（identity / catalog / content / tasking / mcp）
  engine/         # Pi 引擎集成层（Docker 传输 / 扩展生成 / 平台工具常量表；V1 冻结面保留）
  alembic_v2/     # V2 PostgreSQL 迁移链（0001-0014）
  provider_proxy.py  # 按任务令牌路由的薄代理（独立容器运行）
  middleware/     # 上传门禁
  services/       # task_token / harness_service（check_code_style）
docker/           # control / pi-worker / provider-proxy / docker-socket-proxy /
                  # mcp-sandbox（用户 stdio MCP 一次性容器镜像）
frontend/src/     # pages（用户面 + admin 面）/ components / api/v2 / lib / styles
harness/          # Harness 工具（validate_skill、check_code_style）+ 上下文契约片段
tests/            # 后端测试（v2_* 系列 + 公共夹具；默认不依赖 Docker）
tools/            # seed_v2_admin / seed_v2_demo / migrate_v1_skills / probe / acceptance
data/             # 本地数据卷与 Skill 包样例
```

## 数据库迁移双链

- `alembic_v2.ini` + `backend/alembic_v2/` —— **V2 主链**（PostgreSQL）；
  当前 head `0014`（0012 Provider 去目录化：`user_providers.base_url` + 活跃唯一索引
  重建；0013 用户 MCP 面两表；0014 `tasks.mcp_servers` 挂载快照列）。
- `alembic.ini` + `backend/alembic/` —— **V1 历史链**（SQLite，已冻结）；
  V1 应用面已物理删除，此链仅为历史数据形态留存与 reference，不再演进。

## 文档

设计基线在**仓库上层 `docs/`**（本仓 `docs/` 只保留 V1 历史记录）：

- 上层 `docs/AgentCraft-V2-Public-Beta/` —— V2 契约基线（PRD / Engineering Spec /
  Database Design / Deployment & Ops Spec v2.0.0 + API 补遗 v2.0.1）。
- 上层 `docs/AgentCraft_V2_Progress.md` —— V2 升级进展与问题记录（阶段 0-9 全量交付、
  裁决与遗留清单，v2.1）。
- 上层 `docs/superpowers/` —— `specs/` 实施设计、`plans/` 阶段计划与完成记录、
  `reports/` 手测留痕与 SBOM。
- V1 历史：上层 `docs/AgentCraft-V1/`（PRD / Spec / DB / Scaffold Plan v0.4.x 与原型）；
  本仓 `docs/` 保留 `Scaffold_Progress_v0.12.4.md`、`Development_Record_v0.1.0.md`、
  `phase5_piagent.md`、`DEVELOPMENT_PLAN.md`。

## 已知边界与后续

- **单实例部署**：dispatcher / executor 按单实例语义编排（`V2_TASK` 运行参数可调）；
  水平扩展属部署阶段。
- **部署阶段待接线**：Linux VM + nginx + gVisor、runner-manager 独立服务、
  专用 egress proxy、mail-egress、Ed25519 建连凭据与 mTLS 通道。应用层以
  `SandboxBackend` / `TaskCredential` 两抽象预留，归属划分见 Eng §7.1 应用层对账表。
- **Provider 上游**：须为 OpenAI 兼容 `chat/completions` 端点；
  Provider Proxy 不做 Responses API 转换（Pi 扩展注册 `openai-completions` 协议）。
- **邮件出站**：dev 为 `console` 传输；生产 `mail-egress` 属部署阶段接线。
- **用户自带 MCP**：stdio 命令只在一次性 `mcp-sandbox` 容器内执行（只读 rootfs、
  `cap_drop ALL`、internal 网络、零宿主挂载）；HTTP MCP 经 SSRF 公网校验后控制面直连。
  已知边界：一次性容器无跨调用会话状态；Beta 期 internal 网络出网面为部署阶段
  egress 白名单收口。契约详见上层 `docs/.../API_Supplement_v2.0.1.md` §10.14。
