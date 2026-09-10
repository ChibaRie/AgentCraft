"""V2 BYOK Provider 服务域（PlanD-T6 起）。

契约出处：Supplement §3（目录只读面 / CRUD / 连通性测试）、Database Design §3
（owner-RLS、软撤联动）、app-layer design §4.2（KeySealer）。本模块纪律：

- 一切 owner 读写要求调用方已置 GUC（owner_session 事务内）；本模块不 commit；
- 统一 404：行缺失/revoked 一律 HTTPException NOT_FOUND（跨用户 RLS 静默 0 行同形）；
- Key 材料红线：明文/密文/DEK 不进日志、错误消息、幂等记录。
"""

import logging
import uuid as _uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.idempotency import begin, store, subject_user
from backend.v2.ids import uuid7
from backend.v2.models import ProviderCatalog, UserProvider
from backend.v2.provider_crypto import key_sealer
from backend.v2.runtime import V2Runtime, owner_session

logger = logging.getLogger("agentcraft.provider")

_NOT_FOUND_DETAIL = {"code": "NOT_FOUND", "message": "资源不存在"}


def _provider_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)


def _out(row: UserProvider, catalog_display_name: str) -> dict:
    """ORM 行 → ProviderOut 形态 dict（裁决 D13 字段清单；key_last4 裸 4 字符）。"""
    return {
        "id": str(row.id),
        "catalog_id": str(row.catalog_id),
        "catalog_display_name": catalog_display_name,
        "model_id": row.model_id,
        "key_last4": row.key_last4,
        "key_version": row.key_version,
        "status": row.status,
        "is_default": row.is_default,
        "created_at": row.created_at.isoformat(),
    }


async def list_catalog(db: AsyncSession) -> list[dict]:
    """启用中的目录条目（服务层过滤——provider_catalog 无 RLS，D11/D13）。"""
    rows = (
        (
            await db.execute(
                select(ProviderCatalog)
                .where(ProviderCatalog.enabled.is_(True))
                .order_by(ProviderCatalog.display_name.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "display_name": r.display_name,
            "allowed_host": r.allowed_host,
            "models": list(r.models),
        }
        for r in rows
    ]


async def list_user_providers(db: AsyncSession) -> list[dict]:
    """owner 事务内列当前用户 active Provider（RLS 限定；revoked 不可见，D13）。"""
    rows = (
        await db.execute(
            select(UserProvider, ProviderCatalog.display_name)
            .join(ProviderCatalog, UserProvider.catalog_id == ProviderCatalog.id)
            .where(UserProvider.status == "active")
            .order_by(UserProvider.created_at.asc())
        )
    ).all()
    return [_out(row, display_name) for row, display_name in rows]


async def get_provider_row(db: AsyncSession, provider_id: str) -> UserProvider:
    """owner 事务内取 active 行：格式非法 → 400（与 D17/create 同形，防裸 ValueError
    落 500 兜底）；缺失/revoked → 统一 404（D13；跨用户 RLS 0 行同形）。"""
    try:
        pid = _uuid.UUID(provider_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "provider_id 不是合法 UUID"},
        ) from exc
    row = (
        await db.execute(
            select(UserProvider).where(
                UserProvider.id == pid,
                UserProvider.status == "active",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _provider_not_found()
    return row


async def get_provider_detail(db: AsyncSession, provider_id: str) -> dict:
    """owner 事务内单行视图（T7/T9 响应复用）。"""
    row = await get_provider_row(db, provider_id)
    catalog = (
        await db.execute(select(ProviderCatalog).where(ProviderCatalog.id == row.catalog_id))
    ).scalar_one()
    return _out(row, catalog.display_name)


ROUTE_CREATE = "/api/v2/providers"

_ACTIVE_ENTRY_UQ = "uq_user_providers_active_entry"
_ONE_DEFAULT_UQ = "uq_user_providers_one_default"

_DUPLICATE_MESSAGE = "已存在相同目录与模型的 Provider"
_DEFAULT_CONFLICT_MESSAGE = "默认 Provider 设置冲突，请重试"


@dataclass(frozen=True)
class Replay:
    """幂等重放载荷（§7 重放优先；端点原样返回，不携带 Set-Cookie）。"""

    status_code: int
    response_json: dict


def _duplicate(message: str) -> AgentCraftError:
    return AgentCraftError(ErrorCode.PROVIDER_DUPLICATE, message, http_status=409)


def _map_integrity_conflict(exc: IntegrityError) -> AgentCraftError:
    """唯一冲突按约束名分流（D4/D5）：默认互斥 vs 活跃条目重复。"""
    if _ONE_DEFAULT_UQ in str(exc):
        return _duplicate(_DEFAULT_CONFLICT_MESSAGE)
    return _duplicate(_DUPLICATE_MESSAGE)


async def create_provider(
    runtime: V2Runtime,
    *,
    user_id: str,
    updates: dict,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """创建 BYOK Provider（裁决 D4/D5/D7/D11）。

    门序：幂等 begin（app 裸会话，route=ROUTE_CREATE）→ owner_session 单事务
    （UUID/目录边界 → enabled 门 → 白名单门 → 重复检查 → 默认互斥 → seal 落库
    → store）。updates 为 ProviderCreateRequest.model_dump(exclude_unset=True)。
    """
    async with runtime.app_factory() as db:
        replay = await begin(
            db,
            subject_hash=subject_user(user_id),
            route=ROUTE_CREATE,
            key=idem_key,
            req_hash=idem_hash,
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])

    try:
        cid = _uuid.UUID(updates["catalog_id"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "catalog_id 不是合法 UUID"},
        ) from exc

    async with owner_session(runtime, user_id) as db:
        catalog = (
            await db.execute(select(ProviderCatalog).where(ProviderCatalog.id == cid))
        ).scalar_one_or_none()
        if catalog is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": "目录条目不存在"},
            )
        if not catalog.enabled:
            raise AgentCraftError(
                ErrorCode.CATALOG_ITEM_DISABLED, "目录条目已停用", http_status=400
            )
        if updates["model_id"] not in list(catalog.models):
            raise AgentCraftError(
                ErrorCode.MODEL_NOT_ALLOWED, "模型不在目录白名单", http_status=400
            )
        dup = (
            await db.execute(
                select(UserProvider.id).where(
                    UserProvider.user_id == _uuid.UUID(user_id),
                    UserProvider.catalog_id == cid,
                    UserProvider.model_id == updates["model_id"],
                    UserProvider.status == "active",
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise _duplicate(_DUPLICATE_MESSAGE)
        if updates.get("is_default") is True:
            await db.execute(
                text(
                    "UPDATE user_providers SET is_default = false "
                    "WHERE user_id = :u AND is_default = true"
                ),
                {"u": user_id},
            )
        new_id = uuid7()
        key_ciphertext, dek_wrapped = key_sealer().seal(updates["api_key"], provider_id=str(new_id))
        db.add(
            UserProvider(
                id=new_id,
                user_id=_uuid.UUID(user_id),
                catalog_id=cid,
                model_id=updates["model_id"],
                key_ciphertext=key_ciphertext,
                dek_wrapped=dek_wrapped,
                key_last4=updates["api_key"][-4:],  # D7：末 4 位原样
                key_version=1,
                status="active",
                is_default=bool(updates.get("is_default")),
            )
        )
        try:
            await db.flush()
        except IntegrityError as exc:
            raise _map_integrity_conflict(exc) from exc
        detail = await get_provider_detail(db, str(new_id))
        await store(
            db,
            subject_hash=subject_user(user_id),
            route=ROUTE_CREATE,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json={"data": detail},
        )
    return detail
