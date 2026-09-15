"""登录 + MFA 挑战 + TOTP 注册服务（Task 11）。

契约出处：task-11-brief + Supplement §2 + 裁决 A9/A11/A13/A14/A15。事务契约
（调用方必读）：

- login（公开端点，无 CSRF、A5 未列入幂等键控）：限流 ``enforce``（app 会话，
  自管事务独立提交，主体 [HMAC(email), HMAC(ip)] 两行）先于**任何**用户查询
  ——429 即短路零查询；admin 会话按 email 查用户（users_admin_read 只读面）；
- 会话创建一律 ``owner_session(runtime, user_id)`` 单事务内 ``create_session``
  （owner-RLS INSERT，GUC = user_id）；本服务的其余 DB 读走 admin 会话（pre-auth
  鸡蛋问题：按 email/挑战 user_id 定位行发生在知道 owner 之前）。

防枚举（A11）：未知账号与已删除账号执行哑验证 ``equalize_login_timing`` 均衡
时序，随后统一 401 ``INVALID_CREDENTIALS``「账号或密码不正确」——与验密失败同
码同文案同状态，响应体逐字节一致；suspended/deleting 状态门文案与 T7
（session_service）钉死文本逐字一致。

admin TOTP 语义：``mfa_secret_enc IS NOT NULL`` 即挑战路径（admin 已配置 TOTP
同样走挑战）；admin 未配置 TOTP 落常规会话（mfa_verified_at 为 NULL）——Phase 7
admin 门负责在持有期拦截，本服务不特判 role。

内存 store（A13 单实例语义）：``challenge_store`` / ``pending_mfa_secrets`` 为
模块级单例，惰性清理（create/get 顺手剔除过期项），``reset()`` 供测试复位。
挑战 TTL 5 分钟、最多 5 次码尝试（第 5 次失败即销毁）、challenge_id =
token_urlsafe(32)；pending secret TTL 10 分钟、按 user_id 键、重复 setup 覆盖。
进程内单例：多 worker 部署下挑战/注册态不跨进程共享，Phase 2 单进程部署为前提
（跨进程共享需迁移 DB/Redis，属后续阶段裁决）。

失败限流（A15）：login/mfa、activate 与 step-up verify（Phase 8 T3，Sup §10.4）
的码验证失败统一走 ``mfa_failure`` scope（主体 [HMAC(user_id)]，10/900s）；enforce
自管事务先于 attempts 计数与 401——超限 429 **替换** 401（契约：429 wins，此时挑
战尝试计数不推进）。挑战未知/过期不写限流事件（尝试计数归挑战存储）。

MFA 验证后 5 分钟窗口内的状态漂移（login 放行后被停用）不做二次门：所得会话在
get_v2_auth 状态门处被拦（suspended/deleting 403），无越权面。
"""

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pyotp
from fastapi import HTTPException
from sqlalchemy import select, text

from backend.config import get_settings
from backend.errors import AgentCraftError, ErrorCode
from backend.utils.crypto import EncryptionError, decrypt_text, encrypt_text, make_keyring
from backend.v2.models import User
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, owner_session
from backend.v2.security import equalize_login_timing, generate_token, verify_password
from backend.v2.session_service import create_session

# 统一失败文案（契约钉死；状态门文案与 session_service（T7）逐字一致）
_INVALID_CREDENTIALS_MESSAGE = "账号或密码不正确"
_MFA_INVALID_MESSAGE = "验证码无效或已过期"
_MFA_NOT_CONFIGURED_MESSAGE = "尚未配置 TOTP 两步验证"
_SUSPENDED_MESSAGE = "账户已被停用"
_DELETING_MESSAGE = "账户注销处理中"
# V1 约定形状（FORBIDDEN 不在 ErrorCode 注册表；A9 裁决钉死）
_ADMIN_DISABLE_FORBIDDEN_DETAIL = {"code": "FORBIDDEN", "message": "管理员不可停用 TOTP"}

_MFA_ISSUER = "AgentCraft"


def mfa_secret_aad(user_id: str) -> str:
    """TOTP secret 信封加密的 AAD（绑定归属用户，防跨行搬用密文）。"""
    return f"agentcraft:users:{user_id}:mfa_secret:v1"


def _mfa_keyring() -> tuple[str, dict[str, bytes]]:
    """现读 MFA_ENCRYPTION_KEY（b64url 32B）→ (active_kid, keyring)（outbox 同款）。

    每次现读不缓存：轮换即时生效、测试可经环境变量注入；未配置抛干净 ValueError。
    """
    raw = get_settings().MFA_ENCRYPTION_KEY
    if not raw:
        raise ValueError("MFA_ENCRYPTION_KEY 未配置（须为 b64url 32 字节密钥）")
    return make_keyring(f"primary:{raw}")


def _invalid_credentials() -> AgentCraftError:
    return AgentCraftError(
        ErrorCode.INVALID_CREDENTIALS, _INVALID_CREDENTIALS_MESSAGE, http_status=401
    )


def _mfa_invalid(status: int) -> AgentCraftError:
    """MFA 统一失败：同码同文案（401 挑战路径 / 400 注册路径）。"""
    return AgentCraftError(ErrorCode.MFA_INVALID, _MFA_INVALID_MESSAGE, http_status=status)


def _mfa_not_configured() -> AgentCraftError:
    """step-up verify 侧未配置 TOTP 显式 400（已认证无枚举面——Sup §10.4 不对称注记）。"""
    return AgentCraftError(
        ErrorCode.MFA_NOT_CONFIGURED, _MFA_NOT_CONFIGURED_MESSAGE, http_status=400
    )


# ---------- A13 内存 store：模块级单例，惰性清理，reset() 供测试复位 ----------


@dataclass(frozen=True)
class _Challenge:
    user_id: str
    created_at: datetime
    attempts: int


@dataclass(frozen=True)
class _PendingSecret:
    secret: str
    created_at: datetime


class MfaChallengeStore:
    """MFA 登录挑战存储：TTL 5 分钟、5 次码上限、challenge_id = token_urlsafe(32)。"""

    def __init__(self, *, ttl_seconds: int = 300, max_attempts: int = 5) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_attempts = max_attempts
        self._entries: dict[str, _Challenge] = {}

    def _prune(self, now: datetime) -> None:
        expired = [key for key, e in self._entries.items() if now - e.created_at >= self._ttl]
        for key in expired:
            del self._entries[key]

    def create(self, user_id: str) -> str:
        """为新挑战分配 id（惰性清理过期项后落库）。"""
        now = datetime.now(timezone.utc)
        self._prune(now)
        challenge_id = generate_token()
        self._entries[challenge_id] = _Challenge(user_id=user_id, created_at=now, attempts=0)
        return challenge_id

    def get(self, challenge_id: str) -> _Challenge | None:
        """未知/过期 → None（惰性清理过期项）。"""
        now = datetime.now(timezone.utc)
        self._prune(now)
        return self._entries.get(challenge_id)

    def record_failure(self, challenge_id: str) -> None:
        """码验证失败：attempts+1；达上限即销毁（后续 get 必未知）。"""
        entry = self._entries.get(challenge_id)
        if entry is None:
            return
        if entry.attempts + 1 >= self._max_attempts:
            del self._entries[challenge_id]
            return
        self._entries[challenge_id] = replace(entry, attempts=entry.attempts + 1)

    def destroy(self, challenge_id: str) -> None:
        """消费即销毁（成功验证/显式作废）。"""
        self._entries.pop(challenge_id, None)

    def reset(self) -> None:
        """测试专用：清空全部挑战。"""
        self._entries.clear()


class PendingMfaSecretStore:
    """TOTP 注册期 secret 存储：TTL 10 分钟、按 user_id 键、重复 setup 覆盖。"""

    def __init__(self, *, ttl_seconds: int = 600) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._entries: dict[str, _PendingSecret] = {}

    def _prune(self, now: datetime) -> None:
        expired = [key for key, e in self._entries.items() if now - e.created_at >= self._ttl]
        for key in expired:
            del self._entries[key]

    def put(self, user_id: str, secret: str) -> None:
        """登记 pending secret（重复 setup 覆盖旧值）。"""
        now = datetime.now(timezone.utc)
        self._prune(now)
        self._entries[user_id] = _PendingSecret(secret=secret, created_at=now)

    def get(self, user_id: str) -> str | None:
        """未登记/已过期 → None（惰性清理过期项）。"""
        now = datetime.now(timezone.utc)
        self._prune(now)
        entry = self._entries.get(user_id)
        return entry.secret if entry is not None else None

    def pop(self, user_id: str) -> None:
        """注册成功即消费（重启用需重新 setup）。"""
        self._entries.pop(user_id, None)

    def reset(self) -> None:
        """测试专用：清空全部 pending secret。"""
        self._entries.clear()


challenge_store = MfaChallengeStore()
pending_mfa_secrets = PendingMfaSecretStore()


# ---------- 响应载荷与业务流程 ----------


@dataclass(frozen=True)
class MfaRequired:
    """TOTP 已启用用户的登录中间态：挑战 id（不建会话、不种 cookie）。"""

    mfa_challenge_id: str


@dataclass(frozen=True)
class LoginSuccess:
    """登录成功产出：响应体 + 会话明文（端点种双 cookie 用）。"""

    body: dict
    session_token: str


def _login_body(*, user_id, email: str, role: str, status: str, csrf_token: str) -> dict:
    return {
        "data": {
            "user": {"id": str(user_id), "email": email, "role": role, "status": status},
            "csrf_token": csrf_token,
        }
    }


async def _start_session(
    runtime: V2Runtime,
    *,
    user_id,
    email: str,
    role: str,
    status: str,
    device_label: str,
    mfa_verified: bool = False,
) -> LoginSuccess:
    """owner 单事务建会话；mfa_verified 时盖 mfa_verified_at（MFA 挑战成功路径）。"""
    async with owner_session(runtime, str(user_id)) as db:
        session_token, csrf_token = await create_session(
            db, user_id=user_id, device_label=device_label, mfa_verified=mfa_verified
        )
    return LoginSuccess(
        body=_login_body(
            user_id=user_id, email=email, role=role, status=status, csrf_token=csrf_token
        ),
        session_token=session_token,
    )


async def login(
    runtime: V2Runtime,
    *,
    email: str,
    password: str,
    ip: str,
    device_label: str,
) -> MfaRequired | LoginSuccess:
    """登录主流程（brief 步骤 1-5）；事务契约见模块 docstring。

    email 先规范化为小写（限流 HMAC 主体与账号查找共用同一形态）；限流先于一切
    查询。未知/已删除账号哑验证后统一 401；验密失败同文案；状态门
    suspended/deleting 403（pending/active 放行）；TOTP 已启用返回挑战（不建
    会话），否则建会话。
    """
    normalized = email.strip().lower()

    # 1. 限流（app 会话独立提交；两主体各写一行）——先于任何用户查询
    async with runtime.app_factory() as db:
        await enforce(
            db,
            scope="login",
            subjects=[hmac_subject("email", normalized), hmac_subject("ip", ip)],
        )

    # 2. admin 只读查找（users_admin_read USING(true)）；None/deleted → 哑验证 + 统一 401
    async with runtime.admin_factory() as db:
        user = (await db.execute(select(User).where(User.email == normalized))).scalar_one_or_none()
    if user is None or user.status == "deleted":
        equalize_login_timing(password)  # A11：哑验证均衡账号枚举时序
        raise _invalid_credentials()
    if not verify_password(password, user.password_hash):
        raise _invalid_credentials()

    # 3. 状态门（T7 钉死文案）：suspended/deleting 403；pending/active 放行
    if user.status == "suspended":
        raise AgentCraftError(ErrorCode.ACCOUNT_SUSPENDED, _SUSPENDED_MESSAGE, http_status=403)
    if user.status == "deleting":
        raise AgentCraftError(ErrorCode.ACCOUNT_DELETING, _DELETING_MESSAGE, http_status=403)

    # 4. TOTP 已启用（含 admin）→ 挑战路径：不建会话不种 cookie
    if user.mfa_secret_enc is not None:
        return MfaRequired(mfa_challenge_id=challenge_store.create(str(user.id)))

    # 5. 常规路径：owner 单事务建会话
    return await _start_session(
        runtime,
        user_id=user.id,
        email=user.email,
        role=user.role,
        status=user.status,
        device_label=device_label,
    )


async def _enforce_mfa_failure(runtime: V2Runtime, user_id: str) -> None:
    """码失败限流（A15：login/mfa 与 activate 共用 mfa_failure scope）。

    app 会话独立提交；超限抛 429（替换调用方随后的 401/400——契约：429 wins）。
    """
    async with runtime.app_factory() as db:
        await enforce(db, scope="mfa_failure", subjects=[hmac_subject("user", user_id)])


async def login_mfa(
    runtime: V2Runtime,
    *,
    mfa_challenge_id: str,
    totp_code: str,
    device_label: str,
) -> LoginSuccess:
    """MFA 挑战验证主流程；成功销毁挑战并建 mfa_verified 会话。

    挑战未知/过期/码错/信封不可解统一 401 ``MFA_INVALID``（挑战存储自管尝试计
    数：第 5 次码错即销毁）；码错先 enforce ``mfa_failure``（429 优先于 401）再
    attempts+1。
    """
    entry = challenge_store.get(mfa_challenge_id)
    if entry is None:
        raise _mfa_invalid(401)

    # admin 只读取信封（挑战仅存 user_id，明文 secret 不驻内存——解密按需）
    async with runtime.admin_factory() as db:
        row = (
            await db.execute(
                text("SELECT id, email, role, status, mfa_secret_enc FROM users WHERE id = :u"),
                {"u": entry.user_id},
            )
        ).first()
    if row is None or row.mfa_secret_enc is None:
        raise _mfa_invalid(401)
    try:
        secret = decrypt_text(
            json.loads(row.mfa_secret_enc),
            aad=mfa_secret_aad(str(row.id)),
            keyring=_mfa_keyring()[1],
        )
    except EncryptionError as exc:
        raise _mfa_invalid(401) from exc  # 信封损坏/kid 失效：统一 401，不计失败尝试
    if not pyotp.TOTP(secret).verify(totp_code, valid_window=1):
        await _enforce_mfa_failure(runtime, entry.user_id)  # 429 优先于 401
        challenge_store.record_failure(mfa_challenge_id)  # 达上限即销毁
        raise _mfa_invalid(401)

    challenge_store.destroy(mfa_challenge_id)  # 一次性：成功即销毁
    return await _start_session(
        runtime,
        user_id=row.id,
        email=row.email,
        role=row.role,
        status=row.status,
        device_label=device_label,
        mfa_verified=True,
    )


async def verify_mfa(runtime: V2Runtime, *, user_id: str, session_id: str, totp_code: str) -> dict:
    """step-up MFA 续期（Phase 8 T3，Sup §10.4）：认证态重验 TOTP 刷新 12h 窗。

    已认证用户对当前会话重验 TOTP（普通用户即可，非 admin 门）：正确码 → 200
    ``{data:{mfa_verified:true}}`` 且 ``sessions.mfa_verified_at = now()``（12h 窗
    重置，admin 门③即刻解除——门读每请求新解析的会话快照，无需重登录）；
    未配置 TOTP → 400 ``MFA_NOT_CONFIGURED``（已认证无枚举面，与 login 侧统一 401
    防枚举不对称——§10.4 注记）；码错先 enforce ``mfa_failure``（429 优先于 401）
    再 401 ``MFA_INVALID``。信封不可解统一 401 不计失败（login_mfa 同款）。幂等
    豁免（§10.4）：路由不接幂等 begin，重复提交由 mfa_failure 限流兜底。
    """
    # admin 只读信封（login_mfa 同款：明文 secret 不驻内存——解密按需）
    async with runtime.admin_factory() as db:
        row = (
            await db.execute(
                text("SELECT id, mfa_secret_enc FROM users WHERE id = :u"), {"u": user_id}
            )
        ).first()
    if row is None or row.mfa_secret_enc is None:
        raise _mfa_not_configured()
    try:
        secret = decrypt_text(
            json.loads(row.mfa_secret_enc),
            aad=mfa_secret_aad(str(row.id)),
            keyring=_mfa_keyring()[1],
        )
    except EncryptionError as exc:
        raise _mfa_invalid(401) from exc  # 信封损坏/kid 失效：统一 401，不计失败尝试
    if not pyotp.TOTP(secret).verify(totp_code, valid_window=1):
        await _enforce_mfa_failure(runtime, user_id)  # 429 优先于 401
        raise _mfa_invalid(401)

    # owner 上下文盖 MFA 戳（activate_mfa 同款单句 UPDATE；sessions owner-RLS 放行）
    async with owner_session(runtime, user_id) as db:
        await db.execute(
            text(
                "UPDATE sessions SET mfa_verified_at = now() WHERE id = :sid AND revoked_at IS NULL"
            ),
            {"sid": session_id},
        )
    return {"data": {"mfa_verified": True}}


def setup_mfa(*, user_id: str, email: str) -> dict:
    """TOTP 注册第一步：生成 secret 入 pending 存储（不落库），返回 otpauth URI。"""
    secret = pyotp.random_base32()
    pending_mfa_secrets.put(user_id, secret)  # 重复 setup 覆盖
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=_MFA_ISSUER)
    return {"data": {"secret": secret, "otpauth_uri": uri}}


async def activate_mfa(
    runtime: V2Runtime, *, user_id: str, session_id: str, totp_code: str
) -> dict:
    """TOTP 注册第二步：验 pending secret → 信封加密落库 + 当前会话盖 MFA 戳。

    无 pending/码错统一 400 ``MFA_INVALID``；码错先 enforce ``mfa_failure``
    （A15，429 优先于 400）。已启用 TOTP 的用户重新 setup+activate 覆盖
    ``mfa_secret_enc``（设计裁决：可接受）。pending secret 成功即消费。
    """
    secret = pending_mfa_secrets.get(user_id)
    if secret is None:
        raise _mfa_invalid(400)
    if not pyotp.TOTP(secret).verify(totp_code, valid_window=1):
        await _enforce_mfa_failure(runtime, user_id)
        raise _mfa_invalid(400)

    active_kid, keyring = _mfa_keyring()
    envelope = json.dumps(
        encrypt_text(secret, aad=mfa_secret_aad(user_id), keyring=keyring, active_kid=active_kid)
    )
    async with owner_session(runtime, user_id) as db:
        await db.execute(
            text("UPDATE users SET mfa_secret_enc = :e WHERE id = :u"),
            {"e": envelope, "u": user_id},
        )
        await db.execute(
            text(
                "UPDATE sessions SET mfa_verified_at = now() WHERE id = :sid AND revoked_at IS NULL"
            ),
            {"sid": session_id},
        )
    pending_mfa_secrets.pop(user_id)
    return {"data": {"mfa_enabled": True}}


async def disable_mfa(runtime: V2Runtime, *, user_id: str, role: str) -> dict:
    """停用 TOTP：清 ``mfa_secret_enc``。admin 不可停用（A9：403 FORBIDDEN）。"""
    if role == "admin":
        raise HTTPException(status_code=403, detail=dict(_ADMIN_DISABLE_FORBIDDEN_DETAIL))
    async with owner_session(runtime, user_id) as db:
        await db.execute(
            text("UPDATE users SET mfa_secret_enc = NULL WHERE id = :u"), {"u": user_id}
        )
    return {"data": {"mfa_enabled": False}}
