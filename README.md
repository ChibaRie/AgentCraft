# AgentCraft：本地 AI 专家工作台

把领域经验封装为可复用的「专家」：人设 + 方法论 + Skill（能力包）+ MCP 工具，
发布后供他人召唤。每个任务运行在**本机独立 Docker 沙箱**中的 Pi 编码代理里，
模型请求经按任务令牌路由的薄代理转发到你自己配置的 Provider（BYOK）。

> 技术栈：FastAPI + SQLAlchemy(SQLite) + Alembic ｜ React + Vite ｜
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
浏览器 ──HTTP/SSE──> agentcraft-control（FastAPI 控制面）
                        │  SQLite / 文件存储 / ExtensionGenerator / SkillLoader
                        ├─ 每任务 docker run ──> pi-task-<id>（只读 rootfs、非 root、
                        │      internal 网络；/workspace rw + /task-files ro + 扩展 ro）
                        │      ├─ 模型请求 ──> provider-proxy（持原始 Key，按任务令牌路由上游）
                        │      └─ MCP 工具回调 ──> 控制面 /internal/mcp/call
                        │                            └─ docker run ──> mcp-sandbox（真实 MCP Server）
                        └─ 后台巡检：空闲回收 / 看门狗 / 总超时 / 崩溃恢复
```

安全边界要点：任务容器仅入 internal 网络、只挂载三处派生路径、能力以
`tasks.mcp_snapshot` 为上限、控制面回调凭任务级 X-Task-Token（实例级失效），
真实 Provider Key 只存在于 provider-proxy 进程内存。

## 快速开始

前置：Python 3.12+、Node 22+、Docker Desktop（沙箱与 MCP 桥需要）。

```bash
# 1) 后端
cp .env.example .env            # 填 SECRET_KEY / OPENAI_API_KEY（系统默认 Provider）
uv sync

# 初始化数据库
alembic upgrade head

# 启动控制面
uvicorn backend.main:app --host 127.0.0.1 --port 8000

# 2) 前端
cd frontend && npm install && npm run dev   # http://127.0.0.1:5173

# 3) 构建沙箱镜像（首次；受限网络可加 npm 镜像 build-arg）
docker compose -f docker/docker-compose.yml --profile pi-worker build pi-worker
docker compose -f docker/docker-compose.yml --profile mcp-sandbox build mcp-sandbox
```

演示数据（可选，幂等）：

```bash
python tools/seed_demo.py                 # 写入 .env 指向的库
python tools/seed_demo.py --database sqlite+aiosqlite:///./demo.db   # 隔离库
# 登录账号：demo / secret123
```

## 测试

```bash
pytest            # 380+ 用例（契约/服务/引擎/沙箱隔离/加密/代理…）
ruff check .      # 代码风格
cd frontend && npm run build
```

## 目录结构

```
backend/
  api/            # 路由层（users/skills/experts/tasks/mcp/providers/internal）
  services/       # 业务层（task/mcp/provider/lifecycle/harness/…）
  engine/         # PiEngine(Manager)/Docker 传输/SkillLoader/EventHandler/MCP 客户端/扩展生成
  models/ alembic/
  provider_proxy.py   # 按任务令牌路由的薄代理（独立容器运行）
docker/           # control / pi-worker / provider-proxy / mcp-sandbox
frontend/src/     # pages（P01-P10）/ components / api / lib
tests/            # 全量测试（含沙箱隔离性与契约占位校验）
tools/            # seed_demo.py 演示种子
docs/             # 开发计划 / 进度 / 开发记录 / 阶段手册
```

## 文档

- `docs/DEVELOPMENT_PLAN.md` — 阶段推进主索引（阶段 0-7 全部完成）
- `docs/Scaffold_Progress_v0.2.0.md` — 各阶段交付与验收证据
- `docs/Development_Record_v0.1.0.md` — 开发日志与决策
- 上游基线（仓库上层 `docs/AgentCraft-V1/`）：PRD v0.4.1 / Engineering Spec v0.4.0 / Database Design v0.4.0

## 已知边界（v1）

- 单实例部署，mutation lock 为进程内 `asyncio.Lock`（spec §7.2.1 教学版决策，不引入 Redis）
- mcp-sandbox 为共享镜像，不挂载任务工作区（文件系统 Server 看到的是沙箱自身目录）
- Provider Proxy 不做 Responses API 转换（Pi 扩展直接注册 completions 协议）
