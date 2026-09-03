"""Provider 配置业务逻辑（P10 BYOK，手册 §7.7 双模式 / DB 设计 §3.12）。

- Key 写入：AES-256-GCM 信封（AAD 绑定归属用户）；密钥环未配置 → 503
- is_default 用户内单选：置位在同一事务清除同用户其余默认
- 删除不阻塞既有任务：任务使用 provider_snapshot（快照自足）；
  tasks.provider_config_id 由 FK ondelete=SET NULL 置空
- 快照组装：build_provider_snapshot(source, ...) —— user 来源含 Key 信封
  密文，system 来源不含密文（Key 在 proxy 侧环境）
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.models.user_provider import UserProvider
from backend.services.user_service import UserSystemError
from backend.utils.crypto import (
    EncryptionError,
    encrypt_text,
    make_keyring,
    mask_key_hint,
    provider_key_aad,
)


class ProviderNotFoundError(UserSystemError):
    status_code = 404
    code = "NOT_FOUND"


class ProviderForbiddenError(UserSystemError):
    status_code = 403
    code = "FORBIDDEN"


class ProviderNameExistsError(UserSystemError):
    status_code = 409
    code = "PROVIDER_NAME_EXISTS"


class EncryptionUnavailableError(UserSystemError):
    status_code = 503
    code = "ENCRYPTION_UNCONFIGURED"


def _keyring(settings: Settings) -> tuple[str, dict[str, bytes]]:
    try:
        return make_keyring(
            settings.MCP_ENCRYPTION_KEYRING, active_kid=settings.MCP_ENCRYPTION_ACTIVE_KID
        )
    except EncryptionError as exc:
        raise EncryptionUnavailableError(str(exc)) from exc


def _encrypt_key(plaintext: str, user_id: int, settings: Settings) -> tuple[str, str]:
    """返回 (信封 JSON 字符串, 掩码提示)。"""
    active_kid, keyring = _keyring(settings)
    try:
        envelope = encrypt_text(
            plaintext, aad=provider_key_aad(user_id), keyring=keyring, active_kid=active_kid
        )
    except EncryptionError as exc:
        raise EncryptionUnavailableError(str(exc)) from exc
    import json

    return json.dumps(envelope, ensure_ascii=False), mask_key_hint(plaintext)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _clear_other_defaults(db: AsyncSession, user_id: int, keep_id: int | None) -> None:
    await db.execute(
        update(UserProvider)
        .where(UserProvider.user_id == user_id, UserProvider.id != keep_id)
        .values(is_default=0)
    )


def provider_payload(row: UserProvider) -> dict:
    """API 响应构造：永不含 Key 明文/密文，仅掩码提示。"""
    return {
        "id": row.id,
        "name": row.name,
        "protocol": row.protocol,
        "base_url": row.base_url,
        "model_id": row.model_id,
        "is_default": bool(row.is_default),
        "api_key_hint": row.api_key_hint,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def list_providers(db: AsyncSession, user_id: int) -> list[UserProvider]:
    rows = await db.execute(
        select(UserProvider)
        .where(UserProvider.user_id == user_id)
        .order_by(UserProvider.is_default.desc(), UserProvider.id.desc())
    )
    return list(rows.scalars())


async def _get_owned(db: AsyncSession, user_id: int, provider_id: int) -> UserProvider:
    """统一 404（不区分不存在/非本人，消除存在性预言，§7.7 口径与任务侧一致）。"""
    row = await db.get(UserProvider, provider_id)
    if row is None or row.user_id != user_id:
        raise ProviderNotFoundError("Provider 配置不存在")
    return row


async def create_provider(
    db: AsyncSession,
    user_id: int,
    *,
    name: str,
    protocol: str,
    base_url: str,
    model_id: str,
    is_default: bool,
    api_key: str | None,
    settings: Settings,
) -> UserProvider:
    duplicate = (
        await db.execute(
            select(UserProvider).where(
                UserProvider.user_id == user_id, UserProvider.name == name
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ProviderNameExistsError(f"配置名「{name}」已存在")

    encrypted = hint = None
    if api_key:
        encrypted, hint = _encrypt_key(api_key, user_id, settings)

    row = UserProvider(
        user_id=user_id,
        name=name,
        protocol=protocol,
        base_url=base_url,
        api_key_encrypted=encrypted,
        api_key_hint=hint,
        model_id=model_id,
        is_default=1 if is_default else 0,
    )
    db.add(row)
    try:
        await db.flush()
        if is_default:
            await _clear_other_defaults(db, user_id, row.id)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await db.refresh(row)
    return row


async def _assert_name_available(
    db: AsyncSession, user_id: int, name: str, exclude_id: int
) -> None:
    duplicate = (
        await db.execute(
            select(UserProvider).where(
                UserProvider.user_id == user_id,
                UserProvider.name == name,
                UserProvider.id != exclude_id,
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ProviderNameExistsError(f"配置名「{name}」已存在")


async def _apply_update_fields(
    db: AsyncSession,
    user_id: int,
    provider_id: int,
    row: UserProvider,
    *,
    name: str | None,
    protocol: str | None,
    base_url: str | None,
    model_id: str | None,
    is_default: bool | None,
    apply_key,
) -> None:
    """字段级更新：查重改名 / 标量赋值 / Key 三态 / 默认互斥。"""
    if name is not None and name != row.name:
        await _assert_name_available(db, user_id, name, provider_id)
        row.name = name
    if protocol is not None:
        row.protocol = protocol
    if base_url is not None:
        row.base_url = base_url
    if model_id is not None:
        row.model_id = model_id
    await apply_key()
    if is_default is True:
        row.is_default = 1
        await db.flush()
        await _clear_other_defaults(db, user_id, row.id)
    elif is_default is False:
        row.is_default = 0


async def update_provider(
    db: AsyncSession,
    user_id: int,
    provider_id: int,
    *,
    name: str | None = None,
    protocol: str | None = None,
    base_url: str | None = None,
    model_id: str | None = None,
    is_default: bool | None = None,
    # 三态：UNSET 哨兵 = 不变；None = 清除；str = 替换
    api_key: str | None = None,
    api_key_provided: bool = False,
    settings: Settings | None = None,
) -> UserProvider:
    row = await _get_owned(db, user_id, provider_id)

    async def apply_key_change(current: UserProvider) -> None:
        """api_key 三态（缺席=不变 / None=清除 / str=替换）。"""
        if not api_key_provided:
            return
        if api_key is None:
            current.api_key_encrypted = None
            current.api_key_hint = None
        else:
            assert settings is not None
            current.api_key_encrypted, current.api_key_hint = _encrypt_key(
                api_key, user_id, settings
            )

    await _apply_update_fields(
        db, user_id, provider_id, row,
        name=name, protocol=protocol, base_url=base_url,
        model_id=model_id, is_default=is_default,
        apply_key=lambda: apply_key_change(row),
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await db.refresh(row)
    return row


async def delete_provider(db: AsyncSession, user_id: int, provider_id: int) -> None:
    row = await _get_owned(db, user_id, provider_id)
    await db.delete(row)
    await db.commit()


async def get_provider(db: AsyncSession, user_id: int, provider_id: int) -> UserProvider:
    return await _get_owned(db, user_id, provider_id)


async def get_user_default(db: AsyncSession, user_id: int) -> UserProvider | None:
    row = await db.execute(
        select(UserProvider).where(UserProvider.user_id == user_id, UserProvider.is_default == 1)
    )
    return row.scalar_one_or_none()


def build_provider_snapshot(
    *,
    source: str,
    protocol: str,
    base_url: str,
    model_id: str,
    user_provider_id: int | None = None,
    api_key_encrypted: str | None = None,
) -> dict:
    """任务级 Provider 快照（创建时冻结）。

    user 来源含 Key 信封密文（重建容器时 proxy 解密转发）；
    system 来源不含密文（Key 在 proxy 侧 .env）。
    """
    import json

    snapshot = {
        "source": source,
        "protocol": protocol,
        "base_url": base_url,
        "model_id": model_id,
        "loaded_at": _now().isoformat(),
    }
    if source == "user":
        snapshot["user_provider_id"] = user_provider_id
        snapshot["api_key_encrypted"] = json.loads(api_key_encrypted) if api_key_encrypted else None
    return snapshot


async def resolve_task_provider(
    db: AsyncSession, user_id: int, provider_config_id: int | None, settings: Settings
) -> tuple[dict, int | None]:
    """任务创建时的 Provider 解析与快照冻结（§7.7 回退链）。

    显式 provider_config_id → 用户 is_default → 系统默认（source=system）。
    显式指定他人/不存在的配置一律 404（不暴露存在性）。
    返回 (snapshot dict, provider_config_id)。
    """
    row: UserProvider | None = None
    if provider_config_id is not None:
        candidate = await db.get(UserProvider, provider_config_id)
        if candidate is None or candidate.user_id != user_id:
            raise ProviderNotFoundError("Provider 配置不存在")
        row = candidate
    else:
        row = await get_user_default(db, user_id)
    if row is None:
        snapshot = build_provider_snapshot(
            source="system",
            protocol=settings.PI_PROVIDER,
            base_url=settings.PI_PROXY_BASE_URL,
            model_id=settings.PI_MODEL,
        )
        return snapshot, None
    snapshot = build_provider_snapshot(
        source="user",
        protocol=row.protocol,
        base_url=row.base_url,
        model_id=row.model_id,
        user_provider_id=row.id,
        api_key_encrypted=row.api_key_encrypted,
    )
    return snapshot, row.id


def provider_summary(snapshot: dict) -> dict:
    """任务详情侧 Provider 摘要：不含任何 Key 形态（密文/明文/hint）。"""
    return {
        "source": snapshot.get("source"),
        "protocol": snapshot.get("protocol"),
        "base_url": snapshot.get("base_url"),
        "model_id": snapshot.get("model_id"),
        "user_provider_id": snapshot.get("user_provider_id"),
        "api_key_set": snapshot.get("api_key_encrypted") is not None,
        "loaded_at": snapshot.get("loaded_at"),
    }


def provider_fingerprint(snapshot: dict) -> str:
    """Provider 指纹：容器重建判定用（协议+端点+模型+密文指纹）。"""
    import hashlib
    import json

    material = {
        "protocol": snapshot.get("protocol"),
        "base_url": snapshot.get("base_url"),
        "model_id": snapshot.get("model_id"),
        "source": snapshot.get("source"),
        # 密文逐字节参与指纹：换 Key 即换指纹（信封含随机 nonce，天然不同）
        "key": snapshot.get("api_key_encrypted"),
    }
    canonical = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
