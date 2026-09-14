# backend/errors.py
"""统一错误码注册表（API Supplement §7）。新增错误码只能在此登记。"""

from enum import Enum


class ErrorCode(str, Enum):
    # —— 邀请与账户 ——
    INVITATION_INVALID = "INVITATION_INVALID"
    INVITATION_EXPIRED = "INVITATION_EXPIRED"
    INVITATION_CONSUMED = "INVITATION_CONSUMED"
    EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"
    ACCOUNT_PENDING = "ACCOUNT_PENDING"
    ACCOUNT_SUSPENDED = "ACCOUNT_SUSPENDED"
    ACCOUNT_DELETING = "ACCOUNT_DELETING"
    # —— 会话 ——
    MFA_INVALID = "MFA_INVALID"
    CSRF_INVALID = "CSRF_INVALID"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    # 401 语义：登录/再认证凭据校验失败（T12/T13 使用；T15 补遗修订记录登记）
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    # —— 幂等 ——
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    # —— 配额（满槽/满配额走 429/queued，不用错误码）——
    QUOTA_DAILY_EXCEEDED = "QUOTA_DAILY_EXCEEDED"
    QUOTA_ACTIVE_EXCEEDED = "QUOTA_ACTIVE_EXCEEDED"
    QUOTA_STORAGE_EXCEEDED = "QUOTA_STORAGE_EXCEEDED"
    # —— 任务与文件 ——
    INPUT_COMMITTED = "INPUT_COMMITTED"
    INPUT_NOT_COMMITTED = "INPUT_NOT_COMMITTED"
    FILE_LIMIT_EXCEEDED = "FILE_LIMIT_EXCEEDED"
    # —— Provider ——
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    CATALOG_ITEM_DISABLED = "CATALOG_ITEM_DISABLED"
    MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
    KEY_VERSION_REVOKED = "KEY_VERSION_REVOKED"
    # 409；同 (user, catalog, model) active 行已存在（D4）；亦用于并发默认互斥冲突
    # （uq_user_providers_one_default，D5，文案区分）
    PROVIDER_DUPLICATE = "PROVIDER_DUPLICATE"
    # —— 治理 ——
    REVISION_NOT_PUBLISHED = "REVISION_NOT_PUBLISHED"
    REVIEW_PENDING = "REVIEW_PENDING"
    TOOL_REVOKED = "TOOL_REVOKED"
    REPORT_INVALID_TARGET = "REPORT_INVALID_TARGET"
    # 409；同一 report 已被处置后再次 resolve（Phase 4 T6）
    REPORT_ALREADY_RESOLVED = "REPORT_ALREADY_RESOLVED"
    ADMIN_REASON_REQUIRED = "ADMIN_REASON_REQUIRED"
    ADMIN_MFA_REQUIRED = "ADMIN_MFA_REQUIRED"
    # —— 任务域（Phase 6 T2，D15 补登；http_status 在抛出点给定，注释为契约默认）——
    TASK_NOT_FOUND = "TASK_NOT_FOUND"  # 404
    # 409；assert_transition / validate_abort_reason（task_state.py 状态机原语）
    TASK_INVALID_TRANSITION = "TASK_INVALID_TRANSITION"
    TASK_ROUND_BUSY = "TASK_ROUND_BUSY"  # 429
    FILE_NOT_FOUND = "FILE_NOT_FOUND"  # 404
    TOOL_CALL_REJECTED = "TOOL_CALL_REJECTED"  # 400
    # —— V1 既有（legacy 保留）——
    PROMPT_TOO_LARGE = "PROMPT_TOO_LARGE"


class AgentCraftError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        http_status: int = 400,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.headers = headers  # 透传附加响应头（如 401 Retry-After）；None = 无附加头
