# AgentCraft 开发记录

**文档类型**：开发记录（面向项目成员与后续阶段的 AI Coding 智能体）
**文档版本**：v0.4.0
**记录周期**：2026-09-02 至 2026-09-03
**项目位置**：`C:\Users\ChibaRie\Desktop\AgentCraft\agentcraft`
**上游文档**：PRD v0.4.1、Engineering Spec v0.4.0、Database Design v0.4.0、Scaffold Plan v0.4.0
**当前状态**：阶段 1-5 + 5.5（Provider BYOK 双模式）完成——闭环二接通真引擎沙箱，用户可自带 Provider（密文存储/任务级快照/指纹重建）；P09 按原型完成功能增强

---

## 1. 总览

正式业务开发按**垂直切片**策略推进：每个阶段交付一条可独立验收的用户价值链路（后端 API → 前端页面 → 端到端验收），而非按层横向铺开。

| 阶段 | 切片 | 提交 | 规模 | 新增测试 |
|---|---|---|---|---|
| 阶段 1 | 用户系统（注册/登录/个人信息/申请专家） | `33e397a` | 28 文件 +2895/-72 | 27 |
| 阶段 2 | Skill 管理（validate/CRUD/发布/下架 + P08） | `5087b73` | 16 文件 +2155/-73 | 62 |
| 阶段 3 | 专家 CRUD + Skill 绑定 + 专家中心 | `78a4a5f` | 12 文件 +2948/-74 | 40 |
| 阶段 4 | 任务数据层 + SSE 链路（EchoEngine 冻结契约 + P09） | `f6f76e5` | 27 文件 +3750/-83 | 47 |
| 阶段 5 | Pi 引擎集成（容器沙箱 + 重播种 + abort） | `d153a22`+`afaae05` | 32 文件 +4300/-120 | 30+5 |
| P09 增强 | 原型功能对齐（md/工具卡片/上下文面板/视口锁定） | `852dbe3` | 9 文件 +1100/-60 | 2 |
| **合计** | **闭环二接通真引擎** | — | **~17200 行净增** | **208** |

当前测试基线：**244 passed / 31 skipped / 共 275 用例**（skipped 均为已实现端点在契约占位测试中的让位，行为由专属测试文件覆盖）。

```
22dfceb (脚手架) → 21c8ac0 (索引) → 33e397a (阶段1) → 5087b73 (阶段2) → 78a4a5f (阶段3) → f6f76e5 (阶段4) → d153a22+afaae05 (阶段5) → 852dbe3 (P09增强)
```

---

## 2. 工程基建（贯穿各阶段）

### 2.1 统一错误信封（阶段 1 建立，全程复用）

所有失败响应统一为 `{error: {code, message}}`（手册 §6.1），由 `main.py` 全局异常处理器实现：

| 处理器 | 行为 |
|---|---|
| `UserSystemError`（领域异常基类） | 服务层抛出带 `status_code`/`code` 的子类，一处注册全网生效 |
| `StarletteHTTPException` | detail 为 dict 时透传 code/message，否则按状态码映射 |
| `RequestValidationError` | **422 → 400** `VALIDATION_ERROR`（规格 §6.1 要求） |
| `Exception`（兜底） | 500 `INTERNAL_ERROR` + 服务端完整堆栈日志，不向客户端泄露内部信息 |

成功响应统一 `{data: ...}`；列表接口 `{data, total, page, size}`。

**错误码注册表（累计）**：
`VALIDATION_ERROR` / `UNAUTHORIZED` / `INVALID_CREDENTIALS` / `USERNAME_EXISTS` / `EMAIL_EXISTS` / `ALREADY_EXPERT` / `FORBIDDEN` / `NOT_FOUND` / `CONFLICT` / `NOT_IMPLEMENTED` / `INTERNAL_ERROR`（用户域）；`SKILL_INVALID` / `SKILL_NOT_PUBLISHED` / `SKILL_STILL_BOUND` / `INVALID_STATE_TRANSITION`（Skill 域）；`EXPERT_PUBLISH_CONDITION` / `EXPERT_STILL_REFERENCED` / `SKILL_ALREADY_BOUND` / `BINDING_NOT_FOUND`（专家域）；`WORKDIR_INVALID` / `WORKDIR_NOT_FOUND` / `EXPERT_NOT_AVAILABLE` / `EXPERT_OFFLINE` / `TASK_ALREADY_STARTED` / `TASK_ROUND_BUSY` / `PROMPT_TOO_LARGE`（任务域，413=系统提示词超 64KiB）；`FILENAME_INVALID` / `FILE_TOO_LARGE` / `FILE_COUNT_EXCEEDED` / `FILE_QUOTA_EXCEEDED` / `FILE_STORAGE_ERROR`（文件域）；SSE 流内错误帧 `ENGINE_ERROR` / `ROUND_TIMEOUT`（引擎/轮超时，recoverable）。

### 2.2 测试基建

- `tests/conftest.py`：每测试独立 SQLite（tmp 文件 + `NullPool` 规避跨事件循环连接复用），`get_db` 依赖覆盖，外键 PRAGMA 与生产一致；`test_db` fixture 暴露 `session_factory` 供播种场景（如 ExpertSkill 绑定行、Task 引用行）
- 严格 TDD：每阶段先写测试看 RED（记录失败原因），再实现转 GREEN。三次阶段均留下 RED 记录（阶段 1：23 个失败；阶段 2：29 个失败；阶段 3：32 个失败）
- 契约占位测试（`test_api_contracts.py`）随实现进度把端点移入 `IMPLEMENTED` 集合并跳过 501 断言，保证"未实现 → 501/401"的契约始终被监控

### 2.3 对抗式审查机制

每阶段结束运行多维度并行审查 Workflow（安全/后端正确性/规格一致性/前端），每条发现由独立"怀疑者"代理对照真实代码验证（默认视为误报），仅修复确认项：

| 轮次 | 代理数 | 确认发现 | 误报否决 | 修复 |
|---|---|---|---|---|
| 阶段 1 | 15 | 11（去重 9） | 0 | 全部修复 |
| 阶段 2 | 14 | 4 | 1 | 全部修复 |
| 阶段 3 | 22 | 14（去重 13） | 2 | 全部修复（含 1 项等待期主动发现） |
| 阶段 4 | 25 | 21（确认 18） | 3 | 15 项修复 |
| 阶段 5 | 提交安全审查 | 2 | 0 | 2 项修复（轮超时逐段重置→整轮 deadline；stdout 行长/队列无界→1MiB 上限+有界队列）；另有实测驱动规格修正 7 项（phase5 文档 §九） |

累计修复的高价值缺陷示例：bcrypt 72 字节截断边界、登录时序侧信道（哑哈希均衡）、`PUT {"field": null}` 触发 NOT NULL 500（两处）、会话恢复竞态、公开 skill_count 泄漏隐藏 Skill 计数、LIKE 通配符未转义、删除确认条残留导致误删路径。

### 2.4 前端设计系统「墨与纸」

黑白灰中性族、墨色为唯一强调、中等对比；层次三重递进（发丝线 → 灰阶渐变 → 分层阴影）；系统字体栈 + Phosphor 图标；CSS 变量 token 化（`styles/global.css`），亮/暗双主题（`prefers-color-scheme`）、`prefers-reduced-motion` 全量降级；动效仅 transform/opacity（入场错峰 rise、滑块切换、弹窗缩放、骨架 shimmer）。形状锁：控件 10px / 卡片 16px / 面板 20px / 徽标全圆角。每阶段经 impeccable 机械检测器扫描（三次均零缺陷）。

---

## 3. 阶段 1：用户系统（第一个垂直切片）

**目标**：PRD §4.1 用户系统 + 手册 §6.2 四端点；前端 P01 + NavBar/UserMenu + Token 持久化 + P05。

### 3.1 交付内容

| 端点 | 行为要点 |
|---|---|
| `POST /api/auth/register` | 201 + 自动登录 token；username/email 去首尾空格后校验（2-30）；password ≥6 且 ≤72；409 `USERNAME_EXISTS`/`EMAIL_EXISTS`（提示含原值，PRD 文案） |
| `POST /api/auth/login` | `login` 接受用户名或邮箱；统一"账号或密码不正确"防枚举；401 `INVALID_CREDENTIALS` |
| `GET /api/users/me` | JWT 门禁；返回 `created_at` |
| `POST /api/users/me/expert` | 申请即通过 role→expert；重复 409 `ALREADY_EXPERT` |

- **认证**：python-jose（HS256，2h）+ passlib/bcrypt（锁 4.0.1 适配 passlib 1.7.4）；`middleware/auth.py` 提供 `get_current_user`（User 实体）与 `get_current_user_id`（薄封装，占位端点沿用）；`middleware/permission.py` 提供 `require_expert_role`（403），供阶段 3 直接复用
- **前端**：AuthContext（localStorage `agentcraft_token` + 挂载时 `/users/me` 恢复会话 + 401 清除 + token 比对防恢复竞态）；api client（Bearer 注入 + 信封解析 + 非 JSON 2xx 守卫）；P01 分栏登录/注册（滑动墨块切换、PRD 精确错误文案、服务端冲突映射到字段）；NavBar + 权限感知 UserMenu（专家才显示我的专家/Skill 管理）；P05 个人中心（申请专家即时生效）；RequireAuth 路由守卫（未登录跳 /login 记录 from，登录后回跳）

### 3.2 顺带修复的脚手架缺陷

- **User 模型缺失 4 个反向 relationship**：Expert/Skill/MCPServer/Task 的 `back_populates` 悬空，任何 ORM mapper 配置即崩（测试在 RED 阶段暴露）
- 本地 dev 库未迁移（`alembic upgrade head` 补齐）

### 3.3 审查修复（9 项）

bcrypt 72 字节截断（密码上限 72）、NUL 字节密码 400/401 分流、登录时序哑哈希均衡、500 兜底信封、tab 切换错误串场、会话恢复竞态、2xx 非 JSON 守卫、applyExpert 丢失 created_at（合并更新）、naive UTC 时区偏移（前端补 Z）。

### 3.4 验收

PRD §4.1.4 六条验收全过（Playwright）：注册自动登录、重复用户名阻断、正确/错误凭证、申请专家即时生效、登出后受保护页面不可达。桌面/移动 × 亮/暗四象限截图留档。

---

## 4. 阶段 2：Skill 管理垂直切片

**目标**：手册 §6.5 全契约 + §9.1 validate_skill + DB 设计 §5.3 状态机；前端 P08 Skill 部分。

### 4.1 validate_skill 校验器（`harness/mcp/validate_skill.py`，30 单测）

纯文本规则校验、零外部依赖、从不执行输入内容：

| # | 检查项 | 级别 |
|---|---|---|
| 1 | 必填完整性（role/goal/steps/output_requirements/constraints） | ERROR |
| 2 | 字段长度 ≤5000 字符 | WARNING |
| 3 | API Key 模式（sk-/ghp_/AKIA/xox/AIza） | ERROR |
| 4 | 危险指令模式（rm -rf/mkfs/dd/del /s/盘符格式化/DROP TABLE/管道执行脚本） | ERROR |
| 5 | 可执行代码模式（import os/subprocess、eval/exec、os.system 等） | WARNING |
| 6 | 越狱模板（[INST]、`<<<`、`</system>`、忽略以上所有指令、现在你是一个，§7.5） | WARNING **并标记不通过** |

`valid = false` 当且仅当存在 ERROR 或越狱命中；输出 `{valid, issues:[{field,rule,level,message}]}`。

### 4.2 Skill API（32 测试）

- CRUD + `/{id}/publish` + `/{id}/offline` + `/{id}/validate` + DELETE，全部专家门禁
- **状态机**：draft → published → offline → published；非法流转 409 `INVALID_STATE_TRANSITION`
- **发布前置**：内容通过 validate_skill（400 `SKILL_INVALID`，message 含前 5 条 issue 摘要）
- **published 编辑同事务校验**：setattr → validate → 失败 rollback 保留原内容 + 400（规格明确要求）
- **删除保护**：`expert_skills` 绑定计数 > 0 → 409 `SKILL_STILL_BOUND`
- 字段规则 PRD §4.4.2：name 2-30 去空格、description 10-200 去空格、role 5-200、长文本 20-5000 且非全空白、input_requirements 可选 ≤5000

### 4.3 审查修复（4 项）

`PUT {"name": null}` 500→400（不可清除字段 str 化，仅 input_requirements 可空）、删除确认条在编辑保存后残留（误删路径）、PUT body 只取表单字段、列表 total 计数修正。

### 4.4 验收

创建 → validate → 发布 → 下架全流程；负例：已发布编辑塞入 `sk-` 密钥 → 弹窗呈现"校验未通过：constraints 包含疑似 API Key"，后端保留原内容（pytest 断言）。

---

## 5. 阶段 3：专家 CRUD 与绑定 + 专家中心（闭环一收口）

**目标**：PRD §4.2 + 手册 §6.3/§6.4 + DB 设计 §5.2；前端 P03/P04/P06/P07；MCP 绑定端点保留 501（随 MCP 阶段）。

### 5.1 专家管理 API（40 测试）

- CRUD：字段规则按 PRD §4.2.2（name 2-30 去空格、description 10-100 去空格、category 六值枚举、persona/methodology 非空白**不设硬上限**、task_examples ≤5 条×50 字符、avatar_url 仅 http/https 格式校验——可达性由前端 img 回退，后端不主动抓取避免 SSRF）
- **发布前置**：至少一个 enabled 绑定且 Skill 当前 published（400 `EXPERT_PUBLISH_CONDITION`）；重复发布 409
- **下架**：仅 published（409）；running 任务回收的钩子留待任务阶段（注释已标注）
- **删除（快照规则）**：任何状态任务引用即 409 `EXPERT_STILL_REFERENCED`（测试播种真实 Task 行验证）；级联删绑定、Skill 不受影响
- **Skill 绑定**：默认 `enabled=false`；仅 published 可绑（400 `SKILL_NOT_PUBLISHED`）；他人 Skill 403；重复 409 `SKILL_ALREADY_BOUND`；enabled=true 防御性内容校验；开关/解绑 404 `BINDING_NOT_FOUND`（绑定优先于归属的 404/403 次序）
- **PUT 部分更新语义**：`task_examples` 键缺席不改、显式 null 清空（先记录键存在性再 pop）；不可清除字段显式 null 一律 400

### 5.2 专家中心 API（§6.4，匿名可访问）

- 列表：仅 published；`?search`（LIKE 转义 `%`/`_`/`\`）+ `?category` + 分页；卡片含 `skill_count`
- 详情：人设/方法论/任务示例/启用 Skill 列表；非 published 404
- **公开口径统一**：enabled 绑定 ∩ Skill 当前 published——离线 Skill 是运行时 kill switch（手册 §7.5），不出现也不计数，卡片与详情数值恒一致

### 5.3 前端四页

- **P06 我的专家**：状态徽标、发布/下架、删除二次确认（409 任务引用提示）
- **P07 专家编辑**：双栏（表单 + SkillBindingPanel）；任务示例动态增删（≤5）；绑定下拉只列**已发布且未绑定**的 Skill；开关滑块（绑定默认关、启用是独立动作）；创建→编辑经路由 state 传递成功通知；表单加载门禁防覆盖输入
- **P03 专家中心**：URL 参数驱动的搜索/分类 chips/分页；骨架屏/空态；错误独占渲染
- **P04 专家详情**：人设/方法论/擅长任务/已启用 Skill；召唤按钮占位（任务阶段开放）；404 与加载失败分流提示

### 5.4 验收：PRD §3.1 闭环一完整走通（Playwright 全新账号）

```
注册 craftsman → 申请专家 → 建专家「周报管家」→ 建 Skill「周报整理术」→ 校验通过
→ 发布 Skill → 绑定（默认未启用 ✓）→ 启用 → 发布专家
→ 专家中心出现卡片（1 个已启用 Skill）→ P04 详情完整
→ 下架 → 从专家中心消失 ✓
```

---

## 6. 阶段 4：任务数据层 + SSE 链路（EchoEngine 冻结契约）

**目标**：手册 §6.6 任务 API + §8.3 SSE 前端链路 + DB 设计 §3.5-§3.8/§5.5/§6；用 Mock 引擎（EchoEngine）把 SSE 事件契约冻结下来，Pi 真引擎阶段只换引擎实现，前端与事件 schema 不动。完整交付细节见 `Scaffold_Progress_v0.2.0.md` §10。

### 6.1 交付内容

- **任务 API**：`GET /api/workspaces`（授权根内目录浏览，resolve 包含性校验拒 junction/符号链接逃逸）；`POST /api/tasks`（workdir 仅接受根内相对路径，派生存储值与 `ck_tasks_workdir` CHECK 绑定规范常量；专家必须 published；**快照冻结**：expert_name/avatar、skill_snapshot = enabled ∩ published 的完整内容 + persona/methodology、mcp_snapshot = v1 空 tools）；列表/详情（详情含 files + messages，id 兜底秒级时间戳次序）
- **消息 SSE**：`POST /api/tasks/{id}/messages` 帧序 `meta → text_delta×N → message_saved → done` 严格按 §6.6；前置校验失败仍返回统一 JSON 信封；created/failed → running 原子流转在 data lock 内完成；用户消息先落库，assistant 完整回复以引擎 final 事件为依据落库（§7.6）
- **文件上传**：仅 `status=created` 且无用户消息；文件名规则（basename/控制字符/255）+ 单文件 20MB + 单次 10 个 + 任务累计 100MB；流式 SHA-256；暂存 → 校验 → 移动 → DB 提交，任一步失败整批补偿；启动巡检清理超时 staging 与孤儿文件；`UploadSizeGuard` 按 Content-Length 在 multipart 预落盘前拒绝超量请求
- **锁语义（§7.2 的进程内替代）**：per-task data lock（配额 TOCTOU + manifest 冻结与首条消息互斥）+ per-task round lock（一轮未结束再发送 429 + `Retry-After`，seq 锁内计算）
- **前端 P09**：`/tasks/new`（专家选择 `?expert=` 预选 + WorkdirSelector 钻取）；`/tasks/{id}` 左侧任务列表 + 消息流式渲染（text_delta 逐字 + 光标动画）+ 附件 chips（首条消息后禁用）；`api/sse.js` SseClient = Fetch + ReadableStream + Bearer（EventSource 仅支持 GET 且无法携带 Authorization，故弃用）

### 6.2 审查修复（15 项，3 项否决）

高价值缺陷：上传配额 TOCTOU + manifest 冻结复核（data lock）、running 并发发送 429（round lock）、multipart 预落盘磁盘 DoS 守卫、Windows junction 逃逸（`is_symlink` 不识别 junction，改用 resolve 包含性）、mime_type 消毒、前端切换任务时旧 SSE 流污染共享状态（CRITICAL：activeTaskIdRef 乱序守卫 + 切换 abort）、对账失败保留乐观回复。

### 6.3 验收：PRD 闭环二完整走通（Playwright）

```
P04 召唤 → 创建任务（workdir 浏览选择）→ 首条消息前上传附件
→ 发消息 → EchoEngine 流式回复 → 多轮对话 → 刷新后历史可查
→ 发送后附件按钮禁用 ✓
```

---

## 7. 阶段 5：Pi 引擎集成（闭环二接通真引擎）

**目标**：手册 §7 全链路——PiEngine 协议层、SkillLoader、EventHandler、PiEngineManager（容器池/重播种/abort），替换 EchoEngine。执行手册 `docs/phase5_piagent.md`（含 §九 实测规格修正 7 项）。事实源对齐 pi 0.84.3 源码，步骤 1 手工 JSONL 实验录制真实帧序（`tests/fixtures/pi_frames/`）作协议测试语料。

### 7.1 交付内容（4 核心 + 3 支撑文件）

| 模块 | 要点 |
|---|---|
| `engine/pi_engine.py` | JSONL LF 分帧；pending {id: Future} 自增关联（id 错配→needs_rebuild）；handle_line 三分叉（response→Future / extension_ui_request 2s 自动应答 / 事件→回调）；writer lock 串行 stdin；stdout 独立排空协程（1MiB 行长上限） |
| `engine/event_handler.py` | Pi 事件→SSE（§6.6 冻结 schema）；落库纪律：assistant 仅 stopReason=stop，aborted/error 丢弃（防重播种喂回残缺上下文）；tool_execution_end → role=tool 落库；usage 映射 prompt/completion_tokens |
| `engine/skill_loader.py` | §7.5 组装：专家身份→人设→方法论→Skill（任务级 nonce 边界+同形子串剥离）→TaskFile manifest（独立 nonce+数据声明）→工作规则（项目上下文非更高优先级）→末尾忽略声明；64KiB 上限创建时 413 `PROMPT_TOO_LARGE` |
| `engine/pi_engine_manager.py` | 容器表+ensure_container（惰性创建/needs_rebuild/引擎死亡重建）；**重播种**：新容器首条消息嵌入最近 40 条历史（[历史对话回顾]+[当前消息]，绝不单独发历史防幻影轮）；run_round 整轮 deadline 超时→abort；request_abort 绕 mutation lock；有界轮队列（增量可丢/关键必达）；任务令牌随容器轮换 |
| `engine/docker_transport.py` | `ContainerSpec` 单一事实源（CLI/API 双通道防漂移）：argv 数组直传、三挂载、非 root、只读 rootfs、cap_drop ALL、no-new-privileges、tmpfs（/tmp + ~/.pi）、internal 网络、512MB/1CPU；aiodocker API 通道 + docker CLI stdio 通道（npipe 回退）+ stderr 诊断日志 |
| `engine/extension_generator.py` | §7.4 生成 task.ts：MCP 工具循环（快照写死）+ faux provider 注册块（AGENTCRAFT_PROVIDER=faux） |
| `engine/subprocess_transport.py` | 本地子进程传输（PI_RUNTIME=subprocess，仅开发） |

PI_RUNTIME=auto：docker API→docker CLI→本地子进程依序回退。**EchoEngine 删除**；`POST /api/tasks/{id}/abort` 落地（202，所有权检查后绕锁）。

### 7.2 规格修正（实测驱动，已补记规格 §12 #16/#17 与 phase5 文档 §九）

CLI 无内置 faux（经任务扩展 `pi.registerProvider`+自定义 streamSimple 注册）；abort 后 message_end 仍发（stopReason=aborted）且 agent_settled 照常收尾；只读 rootfs 需 `~/.pi` tmpfs（凭证存储）；`--mount readonly` 语法与绝对路径要求；user 消息也有 message_start/end（落库按 role 过滤）；轮超时整轮 deadline（防逐段重置）；Windows 下以 `node <dist/bundle/cli.js>` 形态直跑。

### 7.3 测试策略

- 协议层用**录制帧序回放**（faux_basic/faux_abort.jsonl）而非手造帧
- manager 用脚本化假 Pi（conftest `FakePiTransport`：ACK→分帧回显 outgoing 消息，可挂起/注入错误/模拟 abort 帧序）——重播种/幻影轮/令牌轮换全部可断言
- 契约测试（test_tasks SSE 47 例）经依赖注入换假 manager，与引擎实现解耦，§6.6 契约持续被监控

### 7.4 验收（PRD §4.5.5，faux + Docker 容器运行时，`acceptance_pi_e2e.py`）

```
创建任务 → 首条消息流式（首条 <10s，非首条 0.03s）→ 多轮引用第 1 轮暗号（内存连续）
→ docker rm -f 容器 → 再发消息 → 自动重建+重播种（回复含回顾壳中的暗号）✓
→ abort：202 → done(aborted) → 半截回复不落库 → 任务保持 running 可继续 ✓
→ docker inspect：仅三挂载/internal 网络/只读 rootfs/cap_drop ALL/非 root/无真实 Key ✓
```

---

## 7A. 阶段 5.5：Provider 双模式 BYOK（2026-09-03 完成）

**目标**（策略变更：Provider 切换从 P1 提入 P0）：用户自带 OpenAI 兼容 Provider（P10 配置，Key 信封加密入库，任务级 `provider_snapshot` 冻结）；未配置回退系统 `.env`；生效时机=容器重建（Skill+Provider 双指纹）。文档基线先行（PRD §1.3/§8+P10、DB §3.12+tasks 双列+「Key 不入 DB」修订、手册 §7.7 重写+§7.2 指纹行+决策 #18、新建 `DEVELOPMENT_PLAN.md` 主控）。

### 交付

- `utils/crypto.py`：AES-256-GCM 信封（§11.3 同款换 AAD，AAD 绑定归属用户；ACTIVE_KID 显式校验；写时派生尾 4 位掩码）
- `user_providers` 表 + `tasks.provider_config_id`/`provider_snapshot`（batch 迁移）；快照自足——删除配置不阻塞既有任务
- `/api/providers` CRUD（Key 仅写入/三态更新/默认互斥/所有权统一 404/密钥环未配置 503）
- 任务创建回退链（显式→用户默认→系统）+ 快照冻结 + 详情 `provider` 摘要（`api_key_set` 布尔）
- `ensure_container()` Provider 指纹：当前生效配置（新鲜解析）vs 容器启动指纹 → 重建+重播种；DB 快照保持冻结
- P10 `/settings/providers` + NavBar 入口 + TaskCreatePage Provider 选择器
- 安全审查修复：ACTIVE_KID 校验、所有权 404 化（消除存在性预言）、base_url URL 解析校验（SSRF 立场：本地 BYOK 允许回环（Ollama），出口防线在阶段 6 proxy）

### 边界

容器 env 的 `OPENAI_BASE_URL` 仍指向 provider-proxy——非 faux 用户 Provider 的真实流量在阶段 6（proxy 按令牌路由 + Responses→Completions 兼容转换）打通；faux 链路与数据/快照/指纹行为已完整验收（含浏览器实测）。

---

## 8. 累计度量

### 8.1 测试矩阵（244 passed / 31 skipped，共 275 用例）

| 测试文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_validate_skill.py` | 30 | 校验器六项检查（参数化负例） |
| `test_users.py` | 27 | 注册/登录/me/expert + 权限依赖 + 边界（72 字节、NUL） |
| `test_skills.py` | 32 | Skill 全契约 + 状态机 + 绑定删除保护 |
| `test_experts.py` | 40 | 专家 CRUD/发布条件/绑定/discover + 公开口径一致性 |
| `test_tasks.py` | 50 | 任务创建/快照冻结/workdir/SSE 帧序/状态机/上传/abort/413/快照摘要 |
| `test_pi_engine.py` | 13 | 协议层：ACK 语义/id 自增与错配/坏行跳过/UI 自动应答/生命周期（真实帧序回放） |
| `test_pi_manager.py` | 9 | 重播种包裹与幻影轮防线/引擎死亡重建/令牌轮换/abort 绕锁 |
| `test_event_handler.py` | 8 | 事件翻译：落库纪律/aborted 丢弃/usage 映射/tool_event |
| `test_skill_loader.py` | 13 | §7.5 组装顺序/nonce 边界/同形剥离/manifest/64KiB |
| `test_api_contracts.py` | 50（31 skipped） | 契约存在性 + 未实现端点 501/401 监控 |
| `test_health/models/migrations/pi_*` | 9 | 脚手架基线 |

### 8.2 代码资产

- 后端：`api/`（auth/users/skills/experts/tasks/files/abort 已实现；mcp/internal 与任务 complete/delete 占位 501）、`services/`（user/skill/expert/task/file/workspace/task_locks）、`engine/`（pi_engine/event_handler/skill_loader/pi_engine_manager/extension_generator/docker_transport/subprocess_transport）、`middleware/`（auth/permission/upload_guard）、`schemas/`、`models/`（11 表）、`harness/mcp/validate_skill.py`；pi-worker 镜像 `agentcraft-pi-worker:0.84.3`
- 前端：P09 对齐原型（Markdown 富渲染/思考行/工具调用卡片/右侧三 tab 上下文面板 + Skill 抽屉/运行态指示灯/空态示例 chips/composer 自增高与视口锁定布局）；`lib/markdown.js` 零依赖转义优先渲染器
- 构建：前端 gzip 89.7KB（预算 300KB 内）；ruff 全程零告警；`alembic check` 一致

### 8.3 PRD 验收覆盖状态

| PRD 条目 | 状态 |
|---|---|
| §4.1.4 用户系统验收（6 条） | ✅ 全过 |
| §4.4.6 Skill 验收 | ✅ 创建/编辑/校验/发布/下架/删除保护；运行时上下文加载（第 3-4、8 条）随 Pi 引擎阶段 |
| §4.2.7 专家验收 | ✅ 全过（含「下架后既有任务发送被阻断」，阶段 4 以 `EXPERT_OFFLINE` 落地） |
| §4.3.x 任务验收 | ✅ 闭环二真引擎走通（§7.4：faux 流式/重播种恢复/abort）；complete/delete 随阶段 7 |
| §3.1 闭环一 | ✅ 端到端走通（见 §5.4） |
| §3.2 闭环二 | ✅ 端到端走通（阶段 4 EchoEngine 冻结契约 → 阶段 5 换真引擎，前端零改动） |

---

## 9. 已知边界与阶段 6 接口

当前为后续阶段预留的接缝：

1. **阶段 6（MCP 桥）**：`/internal/mcp/call` 后端实现 + `/api/experts/{id}/mcp` 三端点（仍 501）+ 扩展 TOOLS 真实注入（阶段 5 的 registerTool 模板与任务令牌直接复用）；provider-proxy（faux→openai 切换）；P08 的 MCP 标签页与 P09 右侧 MCP tab 数据接入
2. **阶段 7（生命周期完善）**：任务 complete/delete 端点（仍 501）；mutation lock 跨进程化、并发上限排队、空闲回收、崩溃恢复重试 3 次、Skill 指纹 kill switch、看门狗巡检
3. **规格补记**：`description ≤2000` / `content ≤32000` 为规格未记载的实现上限，待规格升版补记
4. **通用待办**：列表分页在前端仅 P03 有分页控件（P06/P08/P09 侧栏为 size=100/50 + total 计数，课程规模够用）；首页 P02 待数据接入

---

## 10. 变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.4.0 | 2026-09-03 | 增补阶段 5.5（Provider BYOK 双模式）：加密信封/user_providers/CRUD/任务快照/指纹重建/P10；文档基线四份修订；安全审查修复 3 项 |
| v0.3.0 | 2026-09-03 | 增补阶段 5（Pi 引擎集成：协议层/SkillLoader/EventHandler/容器池/重播种/abort + faux 全链路验收）与 P09 原型功能增强（Markdown/工具卡片/上下文面板/视口锁定）；基线 244/31；实测规格修正 7 项 |
| v0.2.0 | 2026-09-03 | 增补阶段 4（任务数据层 + SSE 链路，EchoEngine 冻结契约）：交付内容、审查修复 15 项、闭环二验收；刷新测试基线 204/31、错误码注册表、已知边界 |
| v0.1.0 | 2026-09-03 | 首版：记录阶段 1-3（用户系统 / Skill 管理 / 专家与专家中心）的全部交付、审查修复与验收结果 |
