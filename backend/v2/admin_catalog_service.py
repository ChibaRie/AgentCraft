"""admin 目录管理服务（Phase 7 T6）：目录全量视图 + Provider 目录启停/白名单。

- ``list_tools`` / ``list_providers``：admin 控制台渲染 PUT 目标所需的全量目录
  视图，**含停用条目**（owner 面 provider_service.list_catalog 过滤 enabled 是
  消费门语义，admin 面不过滤——D10-11 契约外补充端点）。tool label 来自控制面
  常量表 PLATFORM_TOOLS（DB 原文无 label 列——platform_tools.py「控制面代码
  自有常量」红线），未登记 (tool_id, version) 组合回退 tool_id。
- ``update_provider``：启停/白名单调整（provider_catalog 无 RLS、0001:1025
  admin blanket ALL 直写）；审计 catalog.provider.update（detail 仅 before/after
  与目录元数据，无任何 Key 材料）；**白名单收缩不回溯存量**（D10 口径
  provider_service.py:41-42——test 路径不做白名单复验，存量 user_providers 行
  与任务零联动）。

授权面：本模块在调用方已 begin 的 admin 会话内执行、零 commit（薄壳纪律）；
admin_db = runtime.admin_factory() 会话。
"""

import uuid as _uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.engine.platform_tools import PLATFORM_TOOLS
from backend.v2.author_service import _reject_invalid_uuid as _parse_uuid
from backend.v2.models import ProviderCatalog
from backend.v2.models.content import AuditLog, ToolCatalog


async def list_tools(admin_db: AsyncSession) -> dict:
    """tool_catalog 全量视图（tool_id/version/label/enabled/permissions）。"""
    rows = (
        (
            await admin_db.execute(
                select(ToolCatalog).order_by(ToolCatalog.tool_id.asc(), ToolCatalog.version.asc())
            )
        )
        .scalars()
        .all()
    )
    items = [
        {
            "tool_id": row.tool_id,
            "version": row.version,
            "label": getattr(PLATFORM_TOOLS.get((row.tool_id, row.version)), "label", row.tool_id),
            "enabled": row.enabled,
            "permissions": row.permissions,
        }
        for row in rows
    ]
    return {"items": items, "total": len(items)}


async def list_providers(admin_db: AsyncSession) -> dict:
    """provider_catalog 全量视图（含停用条目；与 owner 面过滤语义相反）。"""
    rows = (
        (
            await admin_db.execute(
                select(ProviderCatalog).order_by(ProviderCatalog.display_name.asc())
            )
        )
        .scalars()
        .all()
    )
    items = [
        {
            "id": str(row.id),
            "display_name": row.display_name,
            "allowed_host": row.allowed_host,
            "models": list(row.models),
            "enabled": row.enabled,
        }
        for row in rows
    ]
    return {"items": items, "total": len(items)}


def _validate_models(models: list[str]) -> None:
    """白名单条目门：非空字符串（pydantic 已保证 list[str]；空串/空白是坏数据）。"""
    if any(not isinstance(m, str) or not m.strip() for m in models):
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "models 白名单含空条目"},
        )


async def update_provider(
    admin_db: AsyncSession,
    *,
    admin_id: str,
    provider_id: str,
    enabled: bool | None,
    models: list[str] | None,
    reason: str,
    request_id: str | None,
) -> dict:
    """Provider 目录启停/白名单调整（admin 事务内执行，本函数不 commit）。

    enabled/models 均为可选（None = 不变）；至少提供其一。审计
    catalog.provider.update 同事务落库（detail before/after——仅目录元数据，
    无 Key 材料）。白名单为 PUT 整表替换语义；收缩不回溯存量（见模块 docstring）。
    """
    pid = _parse_uuid(provider_id, "provider_id")
    if enabled is None and models is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "至少提供 enabled 或 models 之一"},
        )
    if models is not None:
        _validate_models(models)
    row = (
        await admin_db.execute(
            select(ProviderCatalog).where(ProviderCatalog.id == pid).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail={"code": "NOT_FOUND", "message": "目录条目不存在"}
        )
    detail: dict = {"provider_id": str(row.id), "display_name": row.display_name}
    if enabled is not None:
        detail["enabled_before"] = row.enabled
        detail["enabled_after"] = enabled
        row.enabled = enabled
    if models is not None:
        detail["models_before"] = list(row.models)
        detail["models_after"] = list(models)
        row.models = models
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(str(admin_id)),
            action="catalog.provider.update",
            target_type="provider_catalog",
            target_id=row.id,
            reason=reason,
            request_id=request_id,
            detail=detail,
        )
    )
    await admin_db.flush()
    return {
        "id": str(row.id),
        "display_name": row.display_name,
        "allowed_host": row.allowed_host,
        "models": list(row.models),
        "enabled": row.enabled,
    }
