"""密码重置（request/confirm）+ 密码修改服务（Task 12）。

契约出处：task-12-brief + Supplement §2 + 裁决 A5/A7/A10/A15。事务契约
（调用方必读）：

- request（公开端点，无 CSRF、A5 未列入幂等键控）：限流 ``enforce``（app 会话，
  自管事务独立提交，主体 [HMAC(email), HMAC(ip)] 两行）先于**任何**用户查询
  ——429 即短路零查询。恒 202 防枚举：admin 会话按 email 查用户，status ∈
  ('active','suspended','deleting')（曾验证过邮箱的账户）才发——``owner_session``
  单事务内旧未消费 password_reset 令牌整批作废 → 新令牌（30 分钟，与 outbox
  模板有效期同源 ``VALID_HOURS['password_reset']``）+ outbox 同事务原子落库；
  未知/pending/deleted → 202 零写入；
- confirm（公开端点，无 CSRF；Idempotency-Key 必带 A5，subject=``subject_token``）：
  幂等 ``begin`` 最先（§7 重放优先于一切状态检查——同 key 命中原样重放原 200，
  无论令牌当前已消费与否）；admin 预检按 token_hash + **purpose='password_reset'**
  定位（跨 purpose 令牌不得驱动重置——email_verify/deletion_cancel 统一 400）→
  owner 事务（GUC=令牌行 user_id）：令牌 FOR UPDATE 重校验（未消费未过期，DB
  时钟求值）→ 消费 → ``UPDATE users SET password_hash``（status IN
  ('active','suspended','deleting')，rowcount=0 → 统一 400 分文不消费）→
  ``revoke_all``（**成功撤销该用户全部会话并要求重新登录**）→ 幂等 ``store``
  同事务，任一失败整体回滚。Argon2id 哈希在事务外预算（昂贵 CPU 不持行锁）；
- change（认证端点，get_v2_auth 已过会话/CSRF/状态门；A7 契约缺口补端点）：
  ``mfa_secret_enc`` 非空 → **TOTP 校验先于 Argon2 当前密码校验**（A15：防无
  TOTP 者滥用昂贵计算）——缺码/码错先 enforce ``password_change_totp``（app
  会话独立提交，超限 429 wins 替换 400）再统一 400 ``MFA_INVALID``「验证码无效」
  （与 login 挑战 401 文案「验证码无效或已过期」刻意不同——本端点契约钉死）；
  当前密码验证失败 → 401 ``INVALID_CREDENTIALS``「当前密码不正确」（已认证用户
  无需哑验证——账户存在性已知，无枚举面）；owner 单事务：更新 password_hash →
  撤销**其余**会话（保留当前，改密后其他设备须重新登录）。

防枚举：confirm 全部失效形态（不存在/错 purpose/过期/已消费/状态不符）统一 400
``EMAIL_NOT_VERIFIED``「链接无效或已过期」——同码同文案同状态，响应体逐字节一致
（文案与 email-verification confirm 的「验证链接无效或已过期」刻意区分，均为契约
钉死）。confirm 不设限流（A10 附注同 T10：无文档阈值，凭单次令牌约束——
token_urlsafe(32) 熵 + 30 分钟有效期 + 单次消费 + token_hash 唯一索引）。

密钥说明：TOTP 信封解密的 keyring 读取与 ``login_service._mfa_keyring`` 同一模式
（现读 ``Settings.MFA_ENCRYPTION_KEY`` 不缓存——轮换即时生效、测试可经环境变量
注入）；login_service 未公开该助手，按任务简报「否则复制最小解密面」裁决复制
（信封格式/AAD 归属与 login_service 完全一致，``mfa_secret_aad`` 直接复用其公开
函数）。解密失败（信封损坏/kid 失效）统一 400 且**不**计失败尝试（非用户过错，
与 T11 login_mfa 同裁决）。
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pyotp
from sqlalchemy import select, text

from backend.config import get_settings
from backend.errors import AgentCraftError, ErrorCode
from backend.utils.crypto import EncryptionError, decrypt_text, make_keyring
from backend.v2.idempotency import begin, store, subject_token
from backend.v2.login_service import mfa_secret_aad
from backend.v2.models import AccountActionToken, User
from backend.v2.outbox import VALID_HOURS, enqueue
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, owner_session
from backend.v2.security import generate_token, hash_password, hash_token, verify_password
from backend.v2.session_service import revoke_all

ROUTE_CONFIRM = "/api/auth/password-reset/confirm"
_INVALID_MESSAGE = "链接无效或已过期"  # 契约钉死（brief A10；与验证 confirm 文案区分）
_MFA_INVALID_MESSAGE = "验证码无效"  # 契约钉死（change 端点 400 形态）
_INVALID_CURRENT_MESSAGE = "当前密码不正确"

# 可发起重置/可被重置的账户状态：曾验证过邮箱的账户（pending 未验证、deleted 不发）
_RESETTABLE_STATUSES = frozenset({"active", "suspended", "deleting"})

_RESET_TTL = timedelta(hours=VALID_HOURS["password_reset"])  # 0.5h = 30 分钟

_SELECT_TOKEN_FOR_UPDATE = text(
    "SELECT id, consumed_at, (expires_at > now()) AS live "
    "FROM account_action_tokens "
    "WHERE token_hash = :h AND purpose = 'password_reset' AND user_id = :u FOR UPDATE"
)

_CONSUME_TOKEN_SQL = text(
    "UPDATE account_action_tokens SET consumed_at = now() WHERE id = :id AND consumed_at IS NULL"
)

# confirm：状态过滤 + rowcount 复核（pending/deleted 不可经重置改密；rowcount=0 → 统一 400）
_RESET_PASSWORD_SQL = text(
    "UPDATE users SET password_hash = :h WHERE id = :u "
    "AND status IN ('active', 'suspended', 'deleting')"
)

# change：已过 get_v2_auth 状态门（pending/active），按 id 直更（行不在 = 并发删除，
# 静默 0 行与 T11 activate 同裁决——无越权面）
_CHANGE_PASSWORD_SQL = text("UPDATE users SET password_hash = :h WHERE id = :u")

# request：旧未消费 password_reset 令牌整批作废（防堆积；新令牌另行 INSERT）
_INVALIDATE_PRIOR_SQL = text(
    "UPDATE account_action_tokens SET consumed_at = now() "
    "WHERE user_id = :u AND purpose = 'password_reset' AND consumed_at IS NULL"
)

# change：撤销其余会话（保留当前；行保留供设备列表展示）
_REVOKE_OTHER_SESSIONS_SQL = text(
    "UPDATE sessions SET revoked_at = now() "
    "WHERE user_id = :u AND id <> :current AND revoked_at IS NULL"
)


def _invalid() -> AgentCraftError:
    """统一防枚举错误：所有失效形态同码同文案同状态（响应体逐字节一致）。"""
    return AgentCraftError(ErrorCode.EMAIL_NOT_VERIFIED, _INVALID_MESSAGE, http_status=400)


def _mfa_invalid() -> AgentCraftError:
    return AgentCraftError(ErrorCode.MFA_INVALID, _MFA_INVALID_MESSAGE, http_status=400)


def _invalid_current() -> AgentCraftError:
    return AgentCraftError(ErrorCode.INVALID_CREDENTIALS, _INVALID_CURRENT_MESSAGE, http_status=401)


def _mfa_keyring() -> tuple[str, dict[str, bytes]]:
    """现读 MFA_ENCRYPTION_KEY（b64url 32B）→ (active_kid, keyring)（login_service 同款）。

    每次现读不缓存：轮换即时生效、测试可经环境变量注入；未配置抛干净 ValueError。
    """
    raw = get_settings().MFA_ENCRYPTION_KEY
    if not raw:
        raise ValueError("MFA_ENCRYPTION_KEY 未配置（须为 b64url 32 字节密钥）")
    return make_keyring(f"primary:{raw}")


@dataclass(frozen=True)
class Replay:
    """幂等命中重放载荷：status_code + response_json 原样回放。"""

    status_code: int
    response_json: dict


# ---------- request：恒 202（发信与否对响应不可见）----------


async def request_password_reset(runtime: V2Runtime, *, email: str, ip: str) -> None:
    """重置请求主流程（brief 步骤 1-3）；事务契约见模块 docstring。

    email 规范化为小写（限流 HMAC 主体与账号查找共用同一形态）；限流先于一切
    查询。可重置状态（active/suspended/deleting）→ owner 单事务发信；其余形态
    （未知/pending/deleted）静默返回——调用方恒以 202 ``accepted`` 应答。
    """
    normalized = email.strip().lower()

    # 1. 限流（app 会话独立提交；两主体各写一行）——先于任何用户查询
    async with runtime.app_factory() as db:
        await enforce(
            db,
            scope="password_reset_request",
            subjects=[hmac_subject("email", normalized), hmac_subject("ip", ip)],
        )

    # 2. admin 只读查找；不可重置状态 → 202 零写入（防枚举，不泄账户存在性）
    async with runtime.admin_factory() as db:
        user = (await db.execute(select(User).where(User.email == normalized))).scalar_one_or_none()
    if user is None or user.status not in _RESETTABLE_STATUSES:
        return

    # 3. owner 单事务：旧未消费令牌整批作废 → 新令牌（30 分钟）+ outbox 同事务
    async with owner_session(runtime, str(user.id)) as db:
        await db.execute(_INVALIDATE_PRIOR_SQL, {"u": str(user.id)})
        reset_token = generate_token()
        db.add(
            AccountActionToken(
                user_id=user.id,
                purpose="password_reset",
                token_hash=hash_token(reset_token),
                expires_at=datetime.now(timezone.utc) + _RESET_TTL,
            )
        )
        await enqueue(
            db,
            purpose="password_reset",
            user_id=user.id,
            recipient=user.email,
            action_token=reset_token,
        )


# ---------- confirm：令牌单次消费 + 全会话失效 ----------


async def confirm_password_reset(
    runtime: V2Runtime, *, reset_token: str, new_password: str, idem_key: str, idem_hash: str
) -> dict | Replay:
    """重置确认主流程（brief 步骤 1-4）；事务契约见模块 docstring。

    返回新成功响应体或 ``Replay``（幂等命中：原样重放）；全部失效形态统一抛
    400 ``AgentCraftError(EMAIL_NOT_VERIFIED)``。
    """
    # 1. 幂等 begin 最先（app 会话）：命中且 request_hash 一致 → 原样重放，业务零
    #    副作用——重放优先于一切状态检查（同 key 即令牌已消费也回放原 200，§7）
    subject = subject_token(reset_token)
    async with runtime.app_factory() as db:
        replay = await begin(
            db, subject_hash=subject, route=ROUTE_CONFIRM, key=idem_key, req_hash=idem_hash
        )
    if replay is not None:
        return Replay(status_code=replay["status_code"], response_json=replay["response_json"])

    # 2. admin 预检（鸡蛋问题：user_id 取自令牌行，owner GUC 依赖它）；purpose
    #    过滤钉死——跨 purpose 令牌（email_verify/deletion_cancel）统一 400
    token_hash = hash_token(reset_token)
    async with runtime.admin_factory() as db:
        token_row = (
            await db.execute(
                text(
                    "SELECT user_id FROM account_action_tokens "
                    "WHERE token_hash = :h AND purpose = 'password_reset'"
                ),
                {"h": token_hash},
            )
        ).first()
    if token_row is None:
        raise _invalid()

    # 3. owner 事务（GUC=令牌行 user_id）：锁定重校验 → 消费 → 改密 → 全会话失效
    #    → 幂等 store（Argon2 在事务外预算，昂贵 CPU 不持行锁）
    user_id = str(token_row.user_id)
    body = {"data": {"ok": True}}
    new_hash = hash_password(new_password)
    async with owner_session(runtime, user_id) as db:
        row = (await db.execute(_SELECT_TOKEN_FOR_UPDATE, {"h": token_hash, "u": user_id})).first()
        if row is None or row.consumed_at is not None or not row.live:
            raise _invalid()  # 过期/已消费/不可见 → 统一 400；事务回滚分文不消费

        consumed = await db.execute(_CONSUME_TOKEN_SQL, {"id": row.id})
        if consumed.rowcount != 1:  # belt-and-braces（行已 FOR UPDATE 锁定）
            raise _invalid()

        updated = await db.execute(_RESET_PASSWORD_SQL, {"h": new_hash, "u": user_id})
        if updated.rowcount != 1:  # 状态不符（pending/deleted）→ 统一 400，令牌不消费
            raise _invalid()

        await revoke_all(db, user_id)  # 全部会话失效——要求重新登录（Supplement §2）

        # 幂等 store 同事务原子落库（status_code=200）
        await store(
            db,
            subject_hash=subject,
            route=ROUTE_CONFIRM,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=body,
        )
    return body


# ---------- change：TOTP 门（A15）→ 当前密码 → 其余会话失效 ----------


async def _enforce_change_totp_failure(runtime: V2Runtime, user_id: str) -> None:
    """TOTP 失败限流：app 会话独立提交；超限抛 429（替换随后的 400——429 wins）。"""
    async with runtime.app_factory() as db:
        await enforce(db, scope="password_change_totp", subjects=[hmac_subject("user", user_id)])


async def _verify_change_totp(
    runtime: V2Runtime, *, user_id: str, envelope: str, totp_code: str | None
) -> None:
    """变更端点 TOTP 门（A15：先于 Argon2 当前密码校验，防无 TOTP 者滥用昂贵计算）。

    缺码与码错同形：先 enforce ``password_change_totp``（缺码也计入窗口——跳过
    码字段不构成绕过限流的面）再统一 400 ``MFA_INVALID``；信封解密失败统一 400
    且不计失败尝试（非用户过错，T11 login_mfa 同裁决）。
    """
    if totp_code is None:
        await _enforce_change_totp_failure(runtime, user_id)
        raise _mfa_invalid()
    try:
        secret = decrypt_text(
            json.loads(envelope), aad=mfa_secret_aad(user_id), keyring=_mfa_keyring()[1]
        )
    except EncryptionError as exc:
        raise _mfa_invalid() from exc
    if not pyotp.TOTP(secret).verify(totp_code, valid_window=1):
        await _enforce_change_totp_failure(runtime, user_id)  # 429 优先于 400
        raise _mfa_invalid()


async def change_password(
    runtime: V2Runtime,
    *,
    user_id: str,
    session_id: str,
    current_password_hash: str,
    mfa_secret_enc: str | None,
    current_password: str,
    new_password: str,
    totp_code: str | None,
) -> dict:
    """密码修改主流程（brief 步骤 1-3）；事务契约见模块 docstring。

    TOTP 已启用（``mfa_secret_enc`` 非空）先验码（A15）；当前密码验证失败 401
    ``INVALID_CREDENTIALS``（已认证用户，哑验证不需要）；成功路径 owner 单事务
    更新哈希并撤销**其余**会话（保留当前）。Argon2id 哈希在事务外预算。
    """
    # 1. TOTP 门（先于 Argon2——A15 排序钉死）
    if mfa_secret_enc is not None:
        await _verify_change_totp(
            runtime, user_id=user_id, envelope=mfa_secret_enc, totp_code=totp_code
        )

    # 2. 当前密码验证（用户已认证且存在，无需哑验证）
    if not verify_password(current_password, current_password_hash):
        raise _invalid_current()

    # 3. owner 单事务：更新哈希 → 撤销其余会话（保留当前）
    new_hash = hash_password(new_password)
    async with owner_session(runtime, user_id) as db:
        await db.execute(_CHANGE_PASSWORD_SQL, {"h": new_hash, "u": user_id})
        await db.execute(_REVOKE_OTHER_SESSIONS_SQL, {"u": user_id, "current": session_id})
    return {"data": {"ok": True}}
