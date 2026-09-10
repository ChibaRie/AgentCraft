"""会话服务（Task 7）：不透明会话令牌的创建/解析/滑动刷新/软撤销与认证依赖。

事务契约（调用方必读）：
- ``create_session`` / ``refresh_if_needed`` / ``revoke`` / ``revoke_all`` /
  ``list_sessions`` 均为 **owner-RLS 面**：必须在 ``owner_session``（已设 GUC）
  的事务内调用，本模块不 commit（提交随调用方事务收口）；
- ``resolve_session`` 是 **admin 面**（pre-auth 鸡生蛋：按 token 找会话发生在
  知道 user 之前，owner-RLS 无法表达）——走 ``get_admin_db`` 只读会话；
  admin role 对 sessions 仅有 *_admin_read SELECT policy，天然不可写。
- ``get_v2_auth`` 门序：cookie → resolve（任何失效形态统一 401 SESSION_EXPIRED，
  不泄漏失效模式）→ CSRF（写方法须 X-CSRF-Token，hash 后常量时间比较）→
  状态门（pending/active 放行；suspended/deleting 403；其余 401）→ 滑动刷新
  （仅临界期才开短 owner 事务，写不能走 admin 会话——owner-RLS）。

令牌纪律：会话/CSRF 明文仅 ``create_session`` 返回一次，库存 SHA-256
（``token_hash``/``csrf_hash``）；比较一律 ``constant_time_equals``。
``ac_csrf`` cookie 非 HttpOnly（A14 交付信道：SPA 必须可读），
其余属性与会话 cookie 一致（secure=Settings.SESSION_COOKIE_SECURE、
samesite=strict、path=/、max_age=SESSION_ABSOLUTE）。
"""

import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.v2.models import Session, User
from backend.v2.runtime import V2Runtime, get_admin_db, get_v2_runtime, owner_session
from backend.v2.security import constant_time_equals, generate_token, hash_token

COOKIE_NAME = "ac_session"
CSRF_COOKIE_NAME = "ac_csrf"
SESSION_TTL = timedelta(days=7)  # 滑动有效期：每次刷新续满
SESSION_ABSOLUTE = timedelta(days=30)  # 绝对上限：自 created_at 起算
REFRESH_THRESHOLD = timedelta(days=6)  # 剩余寿命低于此值才触发刷新

# 单语句参数（秒；make_interval 与 rate_limit 同款绑定，asyncpg float8 编码兼容 int）
_TTL_SECONDS = int(SESSION_TTL.total_seconds())
_ABSOLUTE_SECONDS = int(SESSION_ABSOLUTE.total_seconds())
_THRESHOLD_SECONDS = int(REFRESH_THRESHOLD.total_seconds())

# CSRF 门覆盖的写方法（GET/HEAD/OPTIONS 等只读方法免检）
_CSRF_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# 统一失败文案（契约钉死；模块级常量避免每次构造）
_SESSION_EXPIRED_DETAIL = {"code": "SESSION_EXPIRED", "message": "登录状态已失效，请重新登录"}
_CSRF_INVALID_DETAIL = {"code": "CSRF_INVALID", "message": "CSRF 校验失败"}
_SUSPENDED_DETAIL = {"code": "ACCOUNT_SUSPENDED", "message": "账户已被停用"}
_DELETING_DETAIL = {"code": "ACCOUNT_DELETING", "message": "账户注销处理中"}

_REFRESH_SQL = text(
    "UPDATE sessions SET expires_at = LEAST(now() + make_interval(secs => :ttl), "
    "created_at + make_interval(secs => :absolute)) "
    "WHERE id = :id AND revoked_at IS NULL "
    "AND expires_at - now() < make_interval(secs => :threshold)"
)


async def create_session(
    db: AsyncSession,
    *,
    user_id: _uuid.UUID,
    device_label: str,
    mfa_verified: bool = False,
) -> tuple[str, str]:
    """在已设 GUC 的 owner 事务内建会话；返回 ``(session_token, csrf_token)`` 明文。

    明文仅此一次；库存 sha256。expires_at = now()+7d；mfa_verified 时盖
    mfa_verified_at。INSERT 满足 sessions_app_insert WITH CHECK（user_id=owner）。
    """
    session_token = generate_token()
    csrf_token = generate_token()
    now = datetime.now(timezone.utc)
    db.add(
        Session(
            token_hash=hash_token(session_token),
            csrf_hash=hash_token(csrf_token),
            user_id=user_id,
            device_label=device_label,
            expires_at=now + SESSION_TTL,
            mfa_verified_at=now if mfa_verified else None,
        )
    )
    await db.flush()
    return session_token, csrf_token


async def resolve_session(admin_db: AsyncSession, token: str) -> tuple[Session, User] | None:
    """admin 面单查询：按 token 解析未撤销未过期会话并 JOIN 用户。

    token 缺失/未知/已撤销/已过期/用户已 deleted → None（统一由调用方转 401）。
    """
    if not token:
        return None
    row = (
        await admin_db.execute(
            select(Session, User)
            .join(User, Session.user_id == User.id)
            .where(
                Session.token_hash == hash_token(token),
                Session.revoked_at.is_(None),
                Session.expires_at > func.now(),
            )
        )
    ).first()
    if row is None:
        return None
    session, user = row
    if user.status == "deleted":
        return None
    return session, user


async def refresh_if_needed(db: AsyncSession, session_id: _uuid.UUID) -> None:
    """owner 事务内滑动刷新：仅剩寿 < 6d 时续到 min(now()+7d, created_at+30d)。

    单条件 UPDATE（条件内联 WHERE）：阈值外为无锁命中 0 行的空操作。
    """
    await db.execute(
        _REFRESH_SQL,
        {
            "id": session_id,
            "ttl": _TTL_SECONDS,
            "absolute": _ABSOLUTE_SECONDS,
            "threshold": _THRESHOLD_SECONDS,
        },
    )


async def revoke(db: AsyncSession, session_id: _uuid.UUID) -> int:
    """owner 事务内软撤销单个会话（行保留供设备列表展示）；返回受影响行数。"""
    result = await db.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"),
        {"id": session_id},
    )
    return result.rowcount


async def revoke_all(db: AsyncSession, user_id: _uuid.UUID) -> int:
    """owner 事务内软撤销该用户全部未撤销会话（改密/全端登出）；返回行数。"""
    result = await db.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE user_id = :uid AND revoked_at IS NULL"),
        {"uid": user_id},
    )
    return result.rowcount


async def list_sessions(db: AsyncSession, current_id: _uuid.UUID) -> list[dict]:
    """owner 事务内列设备会话：{id, device_label, created_at, expires_at, current}。

    device_label 可为 None（早期会话/未知 UA）；current = id == current_id。
    """
    rows = (
        await db.execute(
            select(
                Session.id, Session.device_label, Session.created_at, Session.expires_at
            ).order_by(Session.created_at.desc(), Session.id)
        )
    ).all()
    return [
        {
            "id": row.id,
            "device_label": row.device_label,
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "current": row.id == current_id,
        }
        for row in rows
    ]


def _cookie_common() -> dict:
    return {
        "secure": get_settings().SESSION_COOKIE_SECURE,
        "samesite": "strict",
        "path": "/",
    }


def set_session_cookie(response: Response, token: str) -> None:
    """种会话 cookie：HttpOnly（JS 不可读，XSS 面收口）+ strict SameSite。"""
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=_ABSOLUTE_SECONDS,
        httponly=True,
        **_cookie_common(),
    )


def set_csrf_cookie(response: Response, csrf_token: str) -> None:
    """种 CSRF cookie：**非 HttpOnly**（SPA 前端读取后回填 X-CSRF-Token，A14）。"""
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=_ABSOLUTE_SECONDS,
        httponly=False,
        **_cookie_common(),
    )


def clear_session_cookie(response: Response) -> None:
    """双 cookie 一并清除（值置空 + max_age=0 即时过期；属性与种入时对齐）。"""
    common = _cookie_common()
    response.delete_cookie(
        COOKIE_NAME, path="/", secure=common["secure"], httponly=True, samesite="strict"
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME, path="/", secure=common["secure"], httponly=False, samesite="strict"
    )


@dataclass(frozen=True)
class V2AuthContext:
    """认证依赖产出：已过全部门序的用户与（刷新前的）会话快照。"""

    user: User
    session: Session


async def get_v2_auth(
    request: Request,
    runtime: V2Runtime = Depends(get_v2_runtime),
    admin_db: AsyncSession = Depends(get_admin_db),
) -> V2AuthContext:
    """FastAPI 认证依赖（自 T4 移入）：cookie 解析 → CSRF 门 → 状态门 → 滑动刷新。

    失效形态（无 cookie/无效/过期/已撤销/deleted 用户）统一 401 SESSION_EXPIRED；
    CSRF 失败 403 CSRF_INVALID；suspended/deleting 403 各自错误码；其余未知状态
    401 兜底。刷新仅临界期开短 owner 事务（owner-RLS 写不能走 admin 会话）。
    """
    token = request.cookies.get(COOKIE_NAME)
    pair = await resolve_session(admin_db, token) if token else None
    if pair is None:
        raise HTTPException(status_code=401, detail=_SESSION_EXPIRED_DETAIL)
    session, user = pair

    if request.method.upper() in _CSRF_METHODS:
        header = request.headers.get("X-CSRF-Token")
        if not header or not constant_time_equals(hash_token(header), session.csrf_hash):
            raise HTTPException(status_code=403, detail=_CSRF_INVALID_DETAIL)

    if user.status == "suspended":
        raise HTTPException(status_code=403, detail=_SUSPENDED_DETAIL)
    if user.status == "deleting":
        raise HTTPException(status_code=403, detail=_DELETING_DETAIL)
    if user.status not in ("pending", "active"):
        raise HTTPException(status_code=401, detail=_SESSION_EXPIRED_DETAIL)

    if session.expires_at - datetime.now(timezone.utc) < REFRESH_THRESHOLD:
        async with owner_session(runtime, str(user.id)) as owner_db:
            await refresh_if_needed(owner_db, session.id)
    return V2AuthContext(user=user, session=session)
