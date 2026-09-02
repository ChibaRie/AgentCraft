# AgentCraft 开发记录

**文档类型**：开发记录（面向项目成员与后续阶段的 AI Coding 智能体）
**文档版本**：v0.1.0
**记录周期**：2026-09-02 至 2026-09-03
**项目位置**：`C:\Users\ChibaRie\Desktop\AgentCraft\agentcraft`
**上游文档**：PRD v0.4.1、Engineering Spec v0.4.0、Database Design v0.4.0、Scaffold Plan v0.4.0
**当前状态**：阶段 1-3 完成，业务闭环一（专家业务闭环）已收口

---

## 1. 总览

正式业务开发按**垂直切片**策略推进：每个阶段交付一条可独立验收的用户价值链路（后端 API → 前端页面 → 端到端验收），而非按层横向铺开。

| 阶段 | 切片 | 提交 | 规模 | 新增测试 |
|---|---|---|---|---|
| 阶段 1 | 用户系统（注册/登录/个人信息/申请专家） | `33e397a` | 28 文件 +2895/-72 | 27 |
| 阶段 2 | Skill 管理（validate/CRUD/发布/下架 + P08） | `5087b73` | 16 文件 +2155/-73 | 62 |
| 阶段 3 | 专家 CRUD + Skill 绑定 + 专家中心 | `78a4a5f` | 12 文件 +2948/-74 | 40 |
| **合计** | **闭环一收口** | — | **~8000 行净增** | **129** |

当前测试基线：**164 passed / 24 skipped**（skipped 均为已实现端点在契约占位测试中的让位，行为由专属测试文件覆盖）。

```
22dfceb (脚手架) → 21c8ac0 (索引) → 33e397a (阶段1) → 5087b73 (阶段2) → 78a4a5f (阶段3)
```

---

## 2. 工程基建（贯穿三阶段）

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
`VALIDATION_ERROR` / `UNAUTHORIZED` / `INVALID_CREDENTIALS` / `USERNAME_EXISTS` / `EMAIL_EXISTS` / `ALREADY_EXPERT` / `FORBIDDEN` / `NOT_FOUND` / `CONFLICT` / `NOT_IMPLEMENTED` / `INTERNAL_ERROR`（用户域）；`SKILL_INVALID` / `SKILL_NOT_PUBLISHED` / `SKILL_STILL_BOUND` / `INVALID_STATE_TRANSITION`（Skill 域）；`EXPERT_PUBLISH_CONDITION` / `EXPERT_STILL_REFERENCED` / `SKILL_ALREADY_BOUND` / `BINDING_NOT_FOUND`（专家域）。

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

## 6. 累计度量

### 6.1 测试矩阵（164 passed / 24 skipped）

| 测试文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_validate_skill.py` | 30 | 校验器六项检查（参数化负例） |
| `test_users.py` | 27 | 注册/登录/me/expert + 权限依赖 + 边界（72 字节、NUL） |
| `test_skills.py` | 32 | Skill 全契约 + 状态机 + 绑定删除保护 |
| `test_experts.py` | 40 | 专家 CRUD/发布条件/绑定/discover + 公开口径一致性 |
| `test_api_contracts.py` | 50（24 skipped） | 契约存在性 + 未实现端点 501/401 监控 |
| `test_health/models/migrations/pi_*` | 9 | 脚手架基线 |

### 6.2 代码资产

- 后端：`api/`（experts/skills/users/auth 已实现；mcp/files/tasks/internal 占位 501）、`services/`（user/skill/expert 已实现）、`middleware/`（auth/permission）、`schemas/`、`models/`（11 表）、`harness/mcp/validate_skill.py`
- 前端：9 页面全部脱离占位（P01/P03/P04/P05/P06/P07/P08 完整；P02 首页、P09 对话页待后续阶段接入数据）；`auth/AuthContext`、`components/`（NavBar/RequireAuth/SkillEditorModal）、`lib/`（datetime/categories）、`api/client`
- 构建：前端 gzip 80KB（预算 300KB 内）；ruff 全程零告警；`alembic check` 一致（relationship 变更不动表结构）

### 6.3 PRD 验收覆盖状态

| PRD 条目 | 状态 |
|---|---|
| §4.1.4 用户系统验收（6 条） | ✅ 全过 |
| §4.4.6 Skill 验收 | ✅ 创建/编辑/校验/发布/下架/删除保护；运行时上下文加载（第 3-4、8 条）留任务阶段 |
| §4.2.7 专家验收 | ✅ 发布条件/下架消失/删除阻断/所有权；「下架后既有任务发送被阻断」（第 4 条）留任务阶段 |
| §3.1 闭环一 | ✅ 端到端走通（见 §5.4） |

---

## 7. 已知边界与阶段 4 接口

当前为后续阶段预留的接缝：

1. **任务阶段接管**：专家下架时的 running 任务回收（`offline_expert` 注释处）、下架专家消息发送拦截（TaskService 按 `expert.status`）、快照机制首次落库（`expert_name_snapshot`/`skill_snapshot`/`mcp_snapshot`）
2. **MCP 管理阶段**：`/api/experts/{id}/mcp` 三端点保持 501；专家详情 `mcps` 字段当前恒为空数组；P08 的 MCP 标签页为占位
3. **Pi 引擎阶段**：`validate_skill` 将被 Pi 上下文组装（SkillLoader）复用；绑定 enabled 语义与运行时 kill switch 的联动
4. **通用待办**：列表分页在前端仅 P03 有分页控件（P06/P08 为 size=100 + total 计数，课程规模够用）；首页 P02 与对话页 P09 待数据接入

---

## 8. 变更记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v0.1.0 | 2026-09-03 | 首版：记录阶段 1-3（用户系统 / Skill 管理 / 专家与专家中心）的全部交付、审查修复与验收结果 |
