"""V2 BYOK Provider 服务域（PlanD-T6 起）。

契约出处：Supplement §3（目录只读面 / CRUD / 连通性测试）、Database Design §3
（owner-RLS、软撤联动）、app-layer design §4.2（KeySealer）。本模块纪律：

- 一切 owner 读写要求调用方已置 GUC（owner_session 事务内）；本模块不 commit；
- 统一 404：行缺失/revoked 一律 HTTPException NOT_FOUND（跨用户 RLS 静默 0 行同形）；
- Key 材料红线：明文/密文/DEK 不进日志、错误消息、幂等记录。
"""

import logging
import uuid as _uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.v2.models import ProviderCatalog, UserProvider

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
