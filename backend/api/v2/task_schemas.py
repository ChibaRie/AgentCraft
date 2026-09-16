"""V2 任务域路由 schema（Phase 6 T8a）。

D14 形状钉死的请求体模型；响应体一律 ``{data: ...}`` 信封由路由层装配，
不设 response_model（与 providers/reports 路由同形态）。extra=forbid 沿用
ProviderCreateRequest 裁决（D14：未声明字段出现即 400，防契约外注入）。
"""

from pydantic import BaseModel, ConfigDict, Field


class TaskCreateRequest(BaseModel):
    """POST /api/tasks 请求体（Sup §4：{expert_revision_id, provider_id,
    initial_message}）。

    provider_id 两态：缺席/None → 走默认位 Provider 解析（resolve_task_provider
    缺省回退，PROVIDER_NOT_CONFIGURED 400）；字符串 → 显式指定（非法/非 active
    400/404 沿现行语义）。provider 快照（D14 四键）由路由层从 ResolvedProvider
    单点构造，客户端不可注入——故本模型无 provider_snapshot 字段。
    """

    model_config = ConfigDict(extra="forbid")

    expert_revision_id: str = Field(min_length=32, max_length=64)
    # 资源卫生上限（服务层按 UTF-8 字节对 Settings.SKILL_PROMPT_MAX_BYTES=65,536
    # 精确校验；此处字符级粗闸仅挡量级异常载荷，先于业务零 DB 副作用）
    initial_message: str = Field(min_length=1, max_length=65536)
    provider_id: str | None = Field(default=None, min_length=32, max_length=64)


class TaskCommitRequest(BaseModel):
    """POST /api/tasks/{id}/input/commit 请求体（Sup §4：manifest 清单）。

    manifest 仅作整体规范化哈希（canonical SHA-256 落 input_manifest_sha256），
    条目结构不做 schema 收紧（T3 服务层契约：零文件任务也必须显式 commit，
    空数组合法）。"""

    model_config = ConfigDict(extra="forbid")

    manifest: list[dict]


class TaskSendMessageRequest(BaseModel):
    """POST /api/tasks/{id}/messages 请求体（Sup §1.2:25：消息正文）。

    content 字符级粗闸仅挡量级异常载荷（先于业务零 DB 副作用）；服务层按
    UTF-8 字节对 Settings.SKILL_PROMPT_MAX_BYTES=65,536 精确校验（D7d，与
    create 同一 ``_validate_initial_message`` 门）。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=65536)
