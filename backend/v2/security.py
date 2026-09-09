"""认证域安全原语：Argon2id、令牌、常量比较、设备摘要。

纪律：令牌仅存 SHA-256；未知账号登录执行哑验证均衡时序（设计裁决 A11，
契约只钉统一失败文案）；本模块零日志（密钥/令牌不落日志红线）。
"""

import hashlib
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()  # argon2id 默认参数（time_cost=3, memory=64MiB, parallelism=4）
DUMMY_PASSWORD_HASH: str = _hasher.hash("agentcraft-timing-equalizer")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, plain)
    except (VerifyMismatchError, ValueError):
        return False


def equalize_login_timing(password: str) -> None:
    """对未知账号执行一次等价 Argon2 计算，消除账号枚举时序侧信道。"""
    try:
        _hasher.verify(DUMMY_PASSWORD_HASH, password)
    except VerifyMismatchError:
        pass


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())


def device_label_from_ua(user_agent: str | None) -> str:
    if not user_agent:
        return "未知设备"
    cleaned = _CONTROL_CHARS.sub(" ", user_agent)
    cleaned = " ".join(cleaned.split())
    return cleaned[:200] or "未知设备"
