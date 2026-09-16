# AgentCraft：本地 AI 专家工作台

[![API](https://img.shields.io/badge/API-2.0.0-blue)](https://github.com/ChibaRie/AgentCraft)
[![Coverage](https://img.shields.io/badge/coverage-88%25-brightgreen)](https://github.com/ChibaRie/AgentCraft)

把领域经验封装为可复用的「专家」：人设 + 方法论 + Skill（能力包）+ 平台工具，
发布后供他人召唤。每个任务运行在**本机独立 Docker 沙箱**中的 Pi 编码代理里，
模型请求经按任务令牌路由的薄代理转发到你自己配置的 Provider（BYOK）。

**当前形态：V2 Public Beta 单轨**——V1 应用面已物理删除，契约路径统一为 `/api/*`
（admin 独立 `/api/admin`），会话单轨 `v2Session`。API 版本 **2.0.0**。

> 技术栈：FastAPI + SQLAlchemy 2.0 + PostgreSQL 16 + Alembic ｜ React 18 + Vite ｜
> [Pi coding-agent](https://github.com/earendil-works/pi-coding-agent)@0.84.3 ｜ Docker

## 功能特性

- **用户与身份**：注册/登录（JWT + bcrypt），一键申请专家身份
- **Skill 管理**：纯文本校验器（validate_skill）→ 发布 → 绑定启用，状态机约束
- **专家与专家中心**：专家 CRUD、发布/下架、公开发现页（P03/P04）
- **任务与流式对话**：召唤专家建任务（快照冻结：人设/Skill/MCP 能力上限/Provider），
  SSE 流式输出（text_delta/tool_event/queued/…），多轮上下文连续（重启重播种）
- **MCP 管理 + MCP 桥**：注册 MCP Server（stdio 沙箱 / streamable HTTP）→
  发现工具（敏感工具需显式授权）→ 发布 → 绑定专家 → Agent 在对话中**真实调用**；
  禁用/下架/撤销授权即时阻断既有任务（kill switch）
- **Provider BYOK**：用户自带 OpenAI 兼容 Key（AES-256-GCM 信封加密入库），
  未配置回退系统默认；真实 Key 永不进容器
- **生命周期完善**：complete/delete/abort（任务级 mutation lock 语义）、并发上限
  （SSE queued 排队）、空闲回收、看门狗 + 任务总超时、崩溃恢复（重建+重播种 ≤3 次）
- **Harness 工具**：`check_code_style`（ruff format/check，/workspace 路径封闭校验）
- **日间/夜间主题**：右上角一键切换，跟随系统默认，无闪烁

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
进程内存（控制面按轮签发 grant，settle 即作废）。

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

## 测试

```bash
# 后端（PostgreSQL 容器或 AGENTCRAFT_TEST_PG_URL 外部库；含覆盖率门槛 80%）
pytest -q --tb=short --cov=backend --cov-report=term-missing --cov-fail-under=80
ruff check backend tests && ruff format --check backend tests

# 前端
cd frontend && npm test && npm run build
```

当前基线（Phase 9 收口）：后端 `pytest` 全绿、`backend/` 语句覆盖率 **88%**；
ruff check + format 双绿；前端 vitest 全绿 + 生产构建通过。
覆盖率排除清单见 `pyproject.toml [tool.coverage.run] omit`（V1 历史迁移链、
`tools/`、V1 冻结面）。

## 目录结构

```
backend/
  api/v2/         # V2 契约路由（auth/account/providers/tasks/discover/authoring/reports）
  api/v2/admin/   # admin 七面（invitations/users/reviews/reports/catalog/audit/tasks）
  v2/             # V2 服务层（登录/会话/RLS 运行时/任务域/治理/审核…）
  v2/models/      # V2 ORM（identity / catalog / content / tasking）
  engine/         # Pi 引擎集成层（Docker 传输 / 扩展生成 / 平台工具常量；V1 冻结面保留）
  alembic_v2/     # V2 PostgreSQL 迁移链（0001-0011）
  provider_proxy.py  # 按任务令牌路由的薄代理（独立容器运行）
docker/           # control / pi-worker / provider-proxy
frontend/src/     # pages（用户面 + admin 面）/ components / api/v2 / lib
tests/            # 后端测试（v2_* 系列 + helpers；无 Docker 依赖）
tools/            # seed_v2_admin.py admin 引导 / seed_v2_demo.py 演示种子
```

## 数据库迁移双链

- `alembic_v2.ini` + `backend/alembic_v2/` —— **V2 主链**（PostgreSQL）；
  当前 head `0011`。
- `alembic.ini` + `backend/alembic/` —— **V1 历史链**（SQLite，已冻结）；
  V1 应用面已物理删除，此链仅为历史数据形态留存与 reference，不再演进。

## 文档

- `docs/AgentCraft-V2-Public-Beta/` —— V2 契约基线（PRD / Engineering Spec /
  Database Design / Deployment & Ops Spec v2.0.0 + API 补遗 v2.0.1）。
- 实现设计与阶段计划见仓库上层 `docs/superpowers/specs|plans/`；
  Phase 9 手测留痕与 SBOM 见 `docs/superpowers/reports/`。
- V1 基线见仓库上层 `docs/AgentCraft-V1/`（历史）。

## 已知边界（V2 Public Beta 应用层）

- 单实例部署：dispatcher/executor 按单实例语义编排（`V2_TASK` 运行参数可调）；
  水平扩展属部署阶段。
- 任务容器由本机 Docker 提供沙箱；Linux VM 形态（gVisor / runner-manager /
  专用 egress）按 Ops Spec 属**部署阶段**，接口以 `SandboxBackend` /
  `TaskCredential` 两抽象预留。
- Provider Proxy 不做 Responses API 转换（Pi 扩展注册 `openai-completions` 协议）；
  上游须为 OpenAI 兼容 `chat/completions` 端点。
- 邮件出站为 `console` 传输（dev）；生产 `mail-egress` 属部署阶段接线。
