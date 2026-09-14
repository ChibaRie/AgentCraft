"""V2 任务令牌（Phase 6 D17）——任务域容器凭据（手册 §7.2 认证行）。

与 V1 令牌（backend/services/task_token.py）共用 ``TASK_TOKEN_SECRET`` 签名密钥，
但以独立 audience（``agentcraft:v2-task-token``）隔离信任域——两族令牌互不可解，
V1 文件零改动。claims = {task_id, owner_id, round_id, lease_epoch, instance,
aud, iat, exp}：

- task_id/owner_id/round_id 为 V2 UUID 串（int 语义属 V1 链，不复用）；
- lease_epoch 随领取下发一次（Eng §3.3:85）：T7 回调校验比对
  ``task_rounds.lease_epoch`` 当前值——被 fence 的旧容器令牌一律拒绝；
- instance 每容器启动随机生成（executor.tokens 登记表比对，D17）。

T7 /internal/tools 四端点校验链：decode → claims.task_id=路径 task_id →
round_id/instance=executor 登记表 → claims.lease_epoch==task_rounds 当前值 →
owner_id 供 D16 owner_session。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from backend.config import get_settings

V2_TASK_TOKEN_AUD = "agentcraft:v2-task-token"

_DEFAULT_TTL_HOURS = 24

# claims 缺一即拒（D17）：字符串五元组 + 整型 lease_epoch 分别把关
_STR_CLAIMS = ("task_id", "owner_id", "round_id", "instance")


class TaskTokenInvalid(Exception):
    """令牌缺失/签名不符/过期/claims 非法。"""


def _token_key(settings) -> str:
    """与 V1 同源密钥（TASK_TOKEN_SECRET，未配置时回退 SECRET_KEY）；aud 隔离信任域。"""
    return settings.TASK_TOKEN_SECRET or settings.SECRET_KEY


def create_v2_task_token(
    *,
    task_id: str,
    owner_id: str,
    round_id: str,
    lease_epoch: int,
    instance: str,
    ttl_hours: int = _DEFAULT_TTL_HOURS,
) -> str:
    """签发 V2 任务令牌（HS256；executor 装配容器时调用并登记 tokens 表）。"""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "task_id": str(task_id),
        "owner_id": str(owner_id),
        "round_id": str(round_id),
        "lease_epoch": int(lease_epoch),
        "instance": str(instance),
        "aud": V2_TASK_TOKEN_AUD,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=ttl_hours)).timestamp()),
    }
    return jwt.encode(payload, _token_key(settings), algorithm=settings.JWT_ALGORITHM)


def decode_v2_task_token(token: str) -> dict:
    """校验并返回 claims；任何失败抛 TaskTokenInvalid（不泄露细节）。

    缺一即拒（D17）：task_id/owner_id/round_id/instance 须为非空字符串，
    lease_epoch 须为整型（bool 是 int 子类，显式排除）。
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            _token_key(settings),
            algorithms=[settings.JWT_ALGORITHM],
            audience=V2_TASK_TOKEN_AUD,
        )
    except JWTError as exc:
        raise TaskTokenInvalid("任务令牌无效") from exc
    claims: dict = {}
    for field in _STR_CLAIMS:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise TaskTokenInvalid("任务令牌 claims 不完整")
        claims[field] = value
    epoch = payload.get("lease_epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool):
        raise TaskTokenInvalid("任务令牌 claims 不完整")
    claims["lease_epoch"] = epoch
    return claims
