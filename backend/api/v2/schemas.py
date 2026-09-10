"""V2 API schema。Task 4 先放空基类；各端点模型由后续任务在此补充。"""

from pydantic import BaseModel, EmailStr, Field


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
