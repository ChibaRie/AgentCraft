"""V2 API schema。Task 4 先放空基类；各端点模型由后续任务在此补充。"""

from pydantic import BaseModel, EmailStr


class V2BaseModel(BaseModel):
    """V2 schema 统一基类（预留 model_config / 统一字段挂载点）。"""


class InvitationAcceptRequest(V2BaseModel):
    """POST /invitations/accept 请求体（Task 9）。

    email 经 pydantic EmailStr（email-validator 同源语法门）边界校验；小写规范化
    在服务层收口（invitation_service.normalize_email），此处不变形。
    """

    invitation_token: str
    email: EmailStr
    password: str
