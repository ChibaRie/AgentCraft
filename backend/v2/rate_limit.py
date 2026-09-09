"""持久化滑动窗口限流（API Supplement §7）：DB 计数 + 机会主义清理。

事务契约（调用方必读）：enforce **自管事务并独立提交**——计数/写入/清理在同一
事务内收口，正常返回即已落库。调用方不得把 enforce 放进自己的 ``begin()`` 块
（事务嵌套提交会 InvalidRequestError）。Task 9 用法形态：pre-auth 端点
（登录/邀请接受/验证码重发等）在**任何业务查询之前**，先以 app-role 会话调用
enforce（rate_limit_events 无 RLS、无需 GUC），429 即短路返回；随后才开启
owner/业务事务——限流提交独立于业务事务，业务回滚不回收已计事件。

主体契约：``subjects`` 为**已哈希**主体串（``hmac_subject`` 的输出，64 hex），
组合维度（如 login = [HMAC(email), HMAC(ip)]）一次调用逐 subject 各写一行；
窗口内任一 subject 达限即整组 429（``= ANY`` 语义），拒绝路径不写事件行。

密钥说明：HMAC 密钥每次调用现读 ``Settings.RATE_LIMIT_HMAC_KEY``（不缓存）——
轮换即时生效、测试可经环境变量注入；未配置/非法材料抛干净 ValueError。
"""

import base64
import binascii
import hashlib
import hmac
import math

from fastapi import HTTPException
from sqlalchemy import ARRAY, String, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.v2.models import RateLimitEvent

# scope 名 → (限值, 窗口秒)。Phase 2 全量注册；Phase 3+ 各域注册各自 scope
# （占位：provider_test / task_create / upload / send_message / report / sse_connect，
# 登记前 enforce 拒绝服务而非静默放行）。
LIMITS: dict[str, tuple[int, int]] = {
    "login": (10, 900),
    "invitation_accept": (5, 3600),
    "email_verify_resend": (3, 3600),
    "password_reset_request": (3, 3600),
    "deletion_cancel_invalid": (10, 3600),  # 无效/伪造探测，主体 [ip]
    "deletion_cancel_attempt": (10, 3600),  # 合法路径尝试，主体 [user, ip]
    "mfa_failure": (10, 900),  # MFA 失败全局限流，主体 [user]
    "password_change_totp": (5, 900),  # TOTP 码失败，主体 [user]
}

_HMAC_KINDS = frozenset({"email", "ip", "user"})

# 单语句聚合：计数 + 窗口内最老事件 + DB 时钟（now() 语句内一致，
# 剩余秒数与窗口过滤共用同一时钟基准，杜绝应用/DB 时钟偏差影响边界）。
_EVENT_COUNT_SQL = text(
    "SELECT count(*) AS cnt, min(occurred_at) AS oldest, now() AS now_ts "
    "FROM rate_limit_events "
    "WHERE scope = :scope AND subject_hash = ANY(:subjects) "
    "AND occurred_at > now() - make_interval(secs => :window)"
).bindparams(bindparam("subjects", type_=ARRAY(String)))

_CLEANUP_SQL = text(
    "DELETE FROM rate_limit_events "
    "WHERE scope = :scope AND occurred_at <= now() - make_interval(secs => :window)"
)


def _rate_limit_hmac_key() -> bytes:
    """读取并校验 RATE_LIMIT_HMAC_KEY（b64url 32B）→ 原始密钥材料。

    校验对齐 config.Settings._validate_v2_secrets 的 b64url padding 解码模式；
    异常只透出干净校验消息，不携带任何输入值 repr（密钥材料不泄日志）。
    """
    raw = get_settings().RATE_LIMIT_HMAC_KEY
    if not raw:
        raise ValueError("RATE_LIMIT_HMAC_KEY 未配置（须为 b64url 编码的 32 字节密钥）")
    try:
        material = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("RATE_LIMIT_HMAC_KEY 不是合法 base64url") from exc
    if len(material) != 32:
        raise ValueError("RATE_LIMIT_HMAC_KEY 解码后必须为 32 字节")
    return material


def hmac_subject(kind: str, value: str) -> str:
    """限流主体派生：``HMAC-SHA256(key, msg=f"{kind}:{value}")`` hexdigest（64 hex）。

    kind ∈ "email" | "ip" | "user"；明文（邮箱/IP）不落库，只落 HMAC 摘要。
    与 hash_token（SHA-256 令牌指纹）是有意不同的原语，不得混用。
    """
    if kind not in _HMAC_KINDS:
        raise ValueError(f"未知限流主体类别: {kind}（仅限 email/ip/user）")
    msg = f"{kind}:{value}".encode("utf-8")
    return hmac.new(_rate_limit_hmac_key(), msg, hashlib.sha256).hexdigest()


async def enforce(db: AsyncSession, *, scope: str, subjects: list[str]) -> None:
    """滑动窗口限流裁决：窗口内计数 ≥ 限值 → 429；否则逐 subject 记事件并提交。

    单事务流：SELECT count（任一 subject 达限即拒）→ INSERT 每 subject 一行 →
    机会主义 DELETE 本 scope 窗口外旧行 → commit。剩余秒数
    = max(1, ceil(window - (now - 最老相关事件)))，经 ``Retry-After`` 头下发。
    """
    if scope not in LIMITS:
        raise ValueError(f"未注册的限流 scope: {scope}（须先在 LIMITS 登记）")
    if not subjects:
        raise ValueError("subjects 不能为空（至少一个限流主体）")
    limit, window = LIMITS[scope]

    row = (
        await db.execute(_EVENT_COUNT_SQL, {"scope": scope, "subjects": subjects, "window": window})
    ).one()
    cnt, oldest, now_ts = row
    if cnt >= limit:
        await db.rollback()  # 释放只读事务；拒绝路径不写事件行
        if oldest is None:  # 理论不可达（cnt >= limit > 0 必有事件行）；防御兜底
            remaining = window
        else:
            remaining = max(1, math.ceil(window - (now_ts - oldest).total_seconds()))
        raise HTTPException(
            status_code=429,
            detail={"code": "TOO_MANY_REQUESTS", "message": "请求过于频繁，请稍后重试"},
            headers={"Retry-After": str(remaining)},
        )

    db.add_all(RateLimitEvent(scope=scope, subject_hash=h) for h in subjects)
    await db.execute(_CLEANUP_SQL, {"scope": scope, "window": window})
    await db.commit()
