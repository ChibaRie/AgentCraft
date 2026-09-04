# AgentCraft 开发计划（主控）

> 阶段推进主索引；各阶段展开版见 `docs/phase5_piagent.md` 等专项手册。
> 状态：✅ 完成 · 🚧 进行中 · ⬜ 未开始。垂直切片策略：每阶段交付一条可独立验收的用户价值链路。

| 阶段 | 切片 | 状态 | 提交/记录 |
|---|---|---|---|
| 0 | 脚手架（FastAPI + React + 11 表 + Docker 骨架） | ✅ | `22dfceb`/`21c8ac0` |
| 1 | 用户系统（注册/登录/身份 + P01/P05） | ✅ | `33e397a` |
| 2 | Skill 管理（validate/CRUD/状态机 + P08） | ✅ | `5087b73` |
| 3 | 专家 CRUD/绑定/专家中心（闭环一收口） | ✅ | `78a4a5f` |
| 4 | 任务数据层 + SSE 链路（EchoEngine 冻结契约 + P09） | ✅ | `f6f76e5` |
| 5 | Pi 引擎集成（容器沙箱/重播种/abort，faux 全链路验收） | ✅ | `d153a22`/`afaae05` |
| P09 增强 | 原型功能对齐（md/工具卡片/上下文面板/视口锁定） | ✅ | `852dbe3` |
| **5.5** | **Provider 双模式 BYOK（用户自带 Key）** | ✅ | 本文档 §阶段 5.5 |
| 6 | MCP 桥 + Provider Proxy 按令牌路由 | ✅ | proxy（2026-09-03 真实模型验收）+ MCP 管理/桥/沙箱（闭环三 E2E：真实模型真实调用 + kill switch 即时阻断 ✅） |
| 7 | 生命周期完善（mutation lock 跨进程/回收/崩溃恢复/看门狗/complete/delete） | ✅ | §7.2.1 锁语义表/并发上限 queued/空闲回收/看门狗+总超时/崩溃恢复/check_code_style/P05+P09（PRD §4.5.7 逐条验收 2026-09-03 ✅） |

---

## 阶段 5.5：Provider 双模式 BYOK（插入于阶段 5 后、阶段 6 前；预估 2-3 天）

**目标**：用户在 P10 设置页配置自有 Provider（OpenAI 兼容端点，如 DeepSeek/Ollama），建任务时选定并冻结 `provider_snapshot`；未配置回退系统 `.env` 默认。与 skill_snapshot 完全同一哲学：**改配置不影响进行中任务，生效时机 = 容器重建（Skill + Provider 双指纹）**。

### 范围内

1. **DB**：新表 `user_providers`（id/user_id/name/protocol/base_url/api_key_encrypted/model_id/is_default）+ `tasks.provider_config_id`/`provider_snapshot`；Alembic 迁移。Key 采用手册 §11.3 同款 AES-256-GCM 信封（密钥环复用 `MCP_ENCRYPTION_*`，AAD=`agentcraft:user_providers:{user_id}:api_key:v1`）——**DB 文档「Key 不入 DB」原则同步修订为「信封加密后入 DB」**
2. **API**：`/api/providers` CRUD（登录门禁；名称用户内唯一；base_url http/https 校验；Key 仅写入、响应只回尾 4 位掩码；is_default 单选事务保证；删除不阻塞快照自足的既有任务）；`POST /api/tasks` 可选 `provider_config_id`（回退链：显式指定 → 用户默认 → 系统默认），快照冻结；任务详情返回 Provider 摘要（不含任何 Key 形态）
3. **指纹重建**：`ensure_container()` 增加 Provider 指纹（当前生效配置 vs 容器启动快照），与 Skill 指纹同轮合并判定，不一致 → teardown + 按快照重建 + 重播种（复用既有机制，零新增组件）
4. **前端 P10**：`/settings/providers` 列表 + 添加/编辑表单（name/protocol/base_url/api_key/model_id/is_default）+ 删除确认 + faux 说明；NavBar 用户菜单入口；`TaskCreatePage` 增加 Provider 选择器（默认项=用户默认/系统默认）
5. **faux 保留**为内置默认测试项（不经 user_providers 表）

### 范围外（归阶段 6）

- **Provider Proxy 按令牌路由升级**：proxy 查任务 → 查 provider_snapshot → 解密 Key → 转发对应 base_url；系统默认模式走 `.env`
- **Responses API 兼容转换**：Pi openai adapter 可能调用 `/v1/responses`，DeepSeek/Ollama 等通常仅实现 `/v1/chat/completions`——proxy 按上游能力转换或回退，直连 404
- 因此 **5.5 完成后、6 完成前**：用户自选非 faux Provider 的任务，容器 env 仍指向 proxy（当前单上游）——真实流量打通以阶段 6 proxy 升级为终点；faux 链路与全部数据/快照/指纹行为在 5.5 内可完整验收

### 验收

- 配置 CRUD 全契约测试 + 加密 round-trip（AAD 绑定、篡改检测、掩码不泄漏）
- 建任务选定 Provider → `provider_snapshot` 冻结；改默认配置 → 进行中任务不受影响
- 下一条消息触发 Provider 指纹不一致 → 容器重建 + 重播种（日志与测试断言）
- 未配置用户走系统默认（现行为不变）；P10 全流程浏览器实测

---

## 阶段 6：MCP 桥 + Proxy 路由（要点）✅ 2026-09-03 完成

- ~~Provider Proxy 按令牌路由 + Responses→Completions 兼容转换~~ **✅ 已随 5.5 提前完成**（2026-09-03 用户实测通过：DeepSeek BYOK 真实对话全链路；Responses 兼容以扩展注册 completions 协议方式规避，无需转换层；Ollama 免钥路径已由测试覆盖，OpenAI 官方上游同一通道）
- `/api/mcp/servers` 九端点 + `/api/experts/{id}/mcp` 三端点 + env 信封（AES-256-GCM，AAD 绑定 server_id）——`mcp_service`
- `/internal/mcp/call`：X-Task-Token 三重校验（签名/任务/实例）+ 快照能力上限 + kill switch 双层（Server/工具/绑定/敏感授权）
- MCP 客户端（stdio 沙箱 + streamable HTTP 双传输）+ `mcp-sandbox` 沙箱镜像（真实 filesystem Server）+ dev 后端转发容器（容器回调控制面通道）
- 前端：P08 MCP Server 标签页 + P07 MCP 绑定面板 + P09 MCP tab 快照渲染
- **闭环三 E2E 验收（2026-09-03）**：真实模型（deepseek-v4-flash BYOK）对话中真实调用 list_directory 并返回真实结果；禁用工具后既有任务下次调用 403 立即阻断；重新启用恢复。详见 Scaffold_Progress §12.2-12.3、Development_Record §7B

## 阶段 7：生命周期完善（要点）✅ 2026-09-03 完成

- ~~complete/delete 端点~~ ✅（§7.2.1 锁语义表逐行：预检→绕锁 abort→等锁→completed；删除持锁先停容器再级联）
- ~~并发上限排队（SSE queued）~~ ✅ 容器槽位信号量；~~空闲回收~~ ✅；~~崩溃恢复重试 3 次~~ ✅（轮中等待事件与容器存活并行，EOF 即崩溃信号）
- ~~Skill/Provider kill switch 巡检~~ ✅（Skill 在阶段 3 发送时校验；Provider/MCP kill switch 于阶段 5.5/6 落地）
- ~~任务总超时看门狗~~ ✅（30s 巡检：容器死亡→failed；总超时→abort+failed+回收）
- ~~check_code_style 内部接口（§6.8）~~ ✅ + 扩展注册块
- ~~P05 个人中心（任务列表）~~ ✅；WorkdirSelector 已于阶段 4 落地（目录浏览，无目录上传）
- mutation lock 跨进程化不实施（§7.2.1：v1 单实例进程内 asyncio.Lock 足够，Redis 不引入）

## 迭代：任务对话附件补传（§6.6 放宽）✅ 2026-09-05 完成

- 上传门禁从「首条消息前」放宽为「任务活跃期（created/running/failed）」；终态（completed/aborted）仍拒绝；`TaskAlreadyStartedError` 废弃，统一 `TaskStateError`（409 INVALID_STATE_TRANSITION）
- 运行中补传的新文件感知：`PiEngine.seeded_file_ids`（容器播种集合）+ `_build_outgoing_message` 末尾追加 `[附件更新]` 块（只列新增、只告知一次）；容器重建路径由 system prompt manifest 全量覆盖，两路收敛
- 前端 P09 `canAttach` 三状态放开 + tooltip 更新
- Spec §3.3/§6.6/§8/§10/验收表共 6 处契约同步；测试：上传放开/终态拒绝 2 例 + 引擎告知块 3 例
