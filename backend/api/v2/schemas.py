"""V2 API schema。Task 4 先放空基类；各端点模型由后续任务在此补充。"""

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class V2BaseModel(BaseModel):
    """V2 schema 统一基类（预留 model_config / 统一字段挂载点）。"""


class InvitationAcceptRequest(V2BaseModel):
    """POST /auth/invitations/accept 请求体（Task 9）。

    email 经 pydantic EmailStr（email-validator 同源语法门）边界校验；小写规范化
    在服务层收口（invitation_service.normalize_email），此处不变形。
    预认证面资源卫生（review round 1）：无长度边界的 invitation_token/password 会
    原样流入 SHA-256/Argon2id，唯一闸门是按 IP 限流（挡不住 IP 轮换）——必须在
    schema 层截断（schema 校验短路于限流依赖与业务，违规请求零 DB 副作用）。
    """

    # 合法 token = token_urlsafe(32) ≈ 43 字符，256 为宽裕上限
    invitation_token: str = Field(max_length=256)
    email: EmailStr
    # 资源卫生上限（Argon2id 输入）；密码最小长度策略待补遗裁决，本任务不设下限
    password: str = Field(max_length=1024)


class EmailVerificationConfirmRequest(V2BaseModel):
    """POST /auth/email-verification/confirm 请求体（Task 10）。

    verify_token 预认证面资源卫生（同 invitation_token 裁决）：无长度边界会原样
    流入 SHA-256（hash_token），必须在 schema 层截断——schema 校验短路于幂等依赖
    与业务，违规请求零 DB 副作用。resend 无请求体（用户上下文来自会话）。
    """

    # 合法 token = token_urlsafe(32) ≈ 43 字符，256 为宽裕上限
    verify_token: str = Field(max_length=256)


class LoginRequest(V2BaseModel):
    """POST /auth/login 请求体（Task 11）。

    email 经 pydantic EmailStr（email-validator 同源语法门）边界校验；小写规范化
    在服务层收口（限流 HMAC 主体与账号查找共用同一形态）。password 资源卫生上限
    同 InvitationAcceptRequest（Argon2id 输入；schema 校验短路于限流与业务）。
    """

    email: EmailStr
    password: str = Field(max_length=1024)


class MfaChallengeRequest(V2BaseModel):
    """POST /auth/login/mfa 请求体（Task 11）：挑战 id + 6-8 位 TOTP 码。"""

    # 合法 challenge_id = token_urlsafe(32) ≈ 43 字符，128 为宽裕上限
    mfa_challenge_id: str = Field(max_length=128)
    totp_code: str = Field(min_length=6, max_length=8)


class MfaActivateRequest(V2BaseModel):
    """POST /auth/mfa/activate 请求体（Task 11）：6-8 位 TOTP 码。"""

    totp_code: str = Field(min_length=6, max_length=8)


class PasswordResetRequestRequest(V2BaseModel):
    """POST /auth/password-reset/request 请求体（Task 12）。

    email 经 pydantic EmailStr（email-validator 同源语法门）边界校验；小写规范化
    在服务层收口（限流 HMAC 主体与账号查找共用同一形态）。
    """

    email: EmailStr


class PasswordResetConfirmRequest(V2BaseModel):
    """POST /auth/password-reset/confirm 请求体（Task 12）。

    reset_token/new_password 预认证面资源卫生（同 T9 invitation_token 裁决）：
    无长度边界会原样流入 SHA-256/Argon2id，必须在 schema 层截断——schema 校验
    短路于幂等依赖与业务，违规请求零 DB 副作用。
    """

    # 合法 token = token_urlsafe(32) ≈ 43 字符，256 为宽裕上限
    reset_token: str = Field(max_length=256)
    # 资源卫生上限（Argon2id 输入）；密码最小长度策略待补遗裁决，本任务不设下限
    new_password: str = Field(max_length=1024)


class PasswordChangeRequest(V2BaseModel):
    """POST /auth/password-change 请求体（Task 12，A7 契约缺口补端点）。

    password 资源卫生上限同 PasswordResetConfirmRequest；totp_code 可选——
    仅 TOTP 已启用用户必填（服务层按 mfa_secret_enc 裁决），6-8 位。
    """

    # 资源卫生上限（Argon2id 输入）；密码最小长度策略待补遗裁决，本任务不设下限
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)
    totp_code: str | None = Field(default=None, min_length=6, max_length=8)


class DeletionRequestRequest(V2BaseModel):
    """POST /account/deletion/request 请求体（Task 13）。

    password 资源卫生上限同 PasswordChangeRequest（Argon2id 输入）；totp_code
    可选——仅 TOTP 已启用用户必填（服务层按 mfa_secret_enc 裁决），6-8 位。
    """

    # 资源卫生上限（Argon2id 输入）；密码最小长度策略待补遗裁决，本任务不设下限
    password: str = Field(max_length=1024)
    totp_code: str | None = Field(default=None, min_length=6, max_length=8)


class DeletionCancelRequest(V2BaseModel):
    """POST /account/deletion/cancel 请求体（Task 13）。

    cancel_token 预认证面资源卫生（同 T9 invitation_token 裁决）：无长度边界会
    原样流入 SHA-256（hash_token），必须在 schema 层截断——schema 校验短路于幂等
    依赖与业务，违规请求零 DB 副作用。
    """

    # 合法 token = token_urlsafe(32) ≈ 43 字符，256 为宽裕上限
    cancel_token: str = Field(max_length=256)
    password: str = Field(max_length=1024)


class ProviderCatalogItem(V2BaseModel):
    """GET /providers/catalog 条目（Sup §3：id、显示名、允许 FQDN、模型白名单）。"""

    id: str
    display_name: str
    allowed_host: str
    models: list[str]


class ProviderOut(V2BaseModel):
    """GET/POST/PUT /providers 的 Provider 行视图（裁决 D13：无任何 Key 材料）。"""

    id: str
    catalog_id: str
    catalog_display_name: str
    model_id: str
    key_last4: str
    key_version: int
    status: str
    is_default: bool
    created_at: str  # ISO8601


class ProviderCreateRequest(V2BaseModel):
    """POST /providers 请求体（Sup §3）。裁决 D14：extra=forbid——base_url/endpoint
    出现即 400（目录化安全边界：用户不可注入端点）。裁决 D7：api_key 8..4096
    （末 4 位入 key_last4，CHECK length=4 的下限保护）。"""

    model_config = ConfigDict(extra="forbid")

    catalog_id: str = Field(min_length=32, max_length=64)
    model_id: str = Field(min_length=1, max_length=200)
    api_key: str = Field(min_length=8, max_length=4096)
    is_default: bool = False
