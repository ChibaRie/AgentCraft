"""User system business logic: registration, authentication, expert application.

错误契约（Engineering Spec §6.1 信封 + PRD §4.1 提示文案）：
服务层抛出 ``UserSystemError`` 子类，由 main.py 的全局异常处理器转换为
``{error: {code, message}}`` 响应。
"""

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.models.user import User

_settings = get_settings()
_password_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 时序均衡哑哈希：未知账号也执行一次 bcrypt 验证，
# 消除"未知账号快、错密码慢"的账号枚举侧信道
_DUMMY_PASSWORD_HASH = _password_context.hash("agentcraft-timing-equalizer")


class UserSystemError(Exception):
    """Base for user-system failures carrying HTTP status and error code."""

    status_code: int = 400
    code: str = "VALIDATION_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UnauthorizedError(UserSystemError):
    status_code = 401
    code = "UNAUTHORIZED"


class InvalidCredentialsError(UnauthorizedError):
    code = "INVALID_CREDENTIALS"


class ConflictError(UserSystemError):
    status_code = 409
    code = "CONFLICT"


class UsernameExistsError(ConflictError):
    code = "USERNAME_EXISTS"


class EmailExistsError(ConflictError):
    code = "EMAIL_EXISTS"


class AlreadyExpertError(ConflictError):
    code = "ALREADY_EXPERT"


def hash_password(password: str) -> str:
    return _password_context.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return _password_context.verify(plain_password, password_hash)
    except ValueError as exc:
        # bcrypt 拒绝 NUL 字节等输入；视为凭证错误而非 500
        raise InvalidCredentialsError("账号或密码不正确") from exc


def create_access_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=_settings.JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, _settings.SECRET_KEY, algorithm=_settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> int:
    """Decode a bearer token into a user id; raises ``UnauthorizedError``."""
    try:
        payload = jwt.decode(token, _settings.SECRET_KEY, algorithms=[_settings.JWT_ALGORITHM])
        return int(payload["sub"])
    except (JWTError, KeyError, TypeError, ValueError) as exc:
        raise UnauthorizedError("登录状态已失效，请重新登录") from exc


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def register_user(db: AsyncSession, username: str, email: str, password: str) -> User:
    """Create a user with role=user. Uniqueness conflicts raise 409."""
    existing_username = await db.scalar(select(User).where(User.username == username))
    if existing_username is not None:
        raise UsernameExistsError(f"用户名 [{username}] 已被使用")

    existing_email = await db.scalar(select(User).where(User.email == email))
    if existing_email is not None:
        raise EmailExistsError(f"邮箱 [{email}] 已被注册")

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role="user",
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        # 并发注册竞态兜底：预检查通过后唯一约束仍可能触发
        await db.rollback()
        raise ConflictError("用户名或邮箱已被使用") from exc
    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, login: str, password: str) -> User:
    """Verify credentials; ``login`` accepts username or email (PRD §4.1.3)."""
    user = await db.scalar(select(User).where((User.username == login) | (User.email == login)))
    if user is None:
        # 对哑哈希执行同等 bcrypt 验证，两条 401 路径耗时一致
        verify_password(password, _DUMMY_PASSWORD_HASH)
        raise InvalidCredentialsError("账号或密码不正确")
    if not verify_password(password, user.password_hash):
        # 统一提示，避免账号枚举（PRD §4.1.3 错误提示）
        raise InvalidCredentialsError("账号或密码不正确")
    return user


async def apply_expert(db: AsyncSession, user: User) -> User:
    """申请即通过（教学版简化策略，PRD §2.1）：role 更新为 expert。"""
    if user.role == "expert":
        raise AlreadyExpertError("已是专家用户，无需重复申请")
    user.role = "expert"
    user.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)
    return user
