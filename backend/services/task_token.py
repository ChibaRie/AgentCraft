"""任务级令牌（手册 §7.2 认证行，§7.7 proxy 按令牌路由）。

JWT（HS256，TASK_TOKEN_SECRET 签名，未配置时回退 SECRET_KEY）：
claims = {task_id, instance, model, exp}。
- provider-proxy 无状态校验：解出 task_id → 查 provider_snapshot → 解密 Key →
  路由上游；model scope 校验（请求体 model 必须与令牌一致）
- instance 每次容器启动随机生成（secrets），容器重建即轮换；旧令牌因无
  撤销列表在有效期内仍可解（v1 本地单操作者可接受，阶段 7 视需要加
  proxy 侧撤销表）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from backend.config import get_settings

_TASK_TOKEN_AUDIENCE = "agentcraft:task-token"
_DEFAULT_TTL_HOURS = 24


class TaskTokenInvalid(Exception):
    """令牌缺失/签名不符/过期/claims 非法。"""


def _token_key(settings) -> str:
    return settings.TASK_TOKEN_SECRET or settings.SECRET_KEY


def create_task_token(
    task_id: int, instance: str, model_id: str, *, ttl_hours: int = _DEFAULT_TTL_HOURS
) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": f"task-{task_id}",
        "task_id": int(task_id),
        "instance": instance,
        "model": model_id,
        "aud": _TASK_TOKEN_AUDIENCE,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=ttl_hours)).timestamp()),
    }
    return jwt.encode(payload, _token_key(settings), algorithm=settings.JWT_ALGORITHM)


def decode_task_token(token: str) -> dict:
    """校验并返回 claims；任何失败抛 TaskTokenInvalid（不泄露细节）。"""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            _token_key(settings),
            algorithms=[settings.JWT_ALGORITHM],
            audience=_TASK_TOKEN_AUDIENCE,
        )
    except JWTError as exc:
        raise TaskTokenInvalid("任务令牌无效") from exc
    task_id = payload.get("task_id")
    instance = payload.get("instance")
    model = payload.get("model")
    if not isinstance(task_id, int) or not instance or not model:
        raise TaskTokenInvalid("任务令牌 claims 不完整")
    return {"task_id": task_id, "instance": instance, "model": model}
