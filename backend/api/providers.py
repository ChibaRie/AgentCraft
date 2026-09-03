"""Provider 配置接口（P10 BYOK，手册 §7.7 双模式）。

全部登录门禁；响应永不含 Key 明文/密文（仅 api_key_hint 掩码）。
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.middleware.auth import get_current_user_id
from backend.schemas.provider import ProviderCreateRequest, ProviderUpdateRequest
from backend.services import provider_service

router = APIRouter(prefix="/providers", tags=["providers"])

# PUT api_key 三态哨兵：区分「缺席（不变）」与「显式 null（清除）」
_UNSET = object()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: ProviderCreateRequest,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    row = await provider_service.create_provider(
        db,
        user_id,
        name=payload.name,
        protocol=payload.protocol,
        base_url=payload.base_url,
        model_id=payload.model_id,
        is_default=payload.is_default,
        api_key=payload.api_key,
        settings=settings,
    )
    return {"data": provider_service.provider_payload(row)}


@router.get("")
async def list_providers(
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    rows = await provider_service.list_providers(db, user_id)
    return {"data": [provider_service.provider_payload(row) for row in rows]}


@router.get("/{provider_id}")
async def get_provider(
    provider_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    row = await provider_service.get_provider(db, user_id, provider_id)
    return {"data": provider_service.provider_payload(row)}


@router.put("/{provider_id}")
async def update_provider(
    provider_id: int,
    payload: ProviderUpdateRequest,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    # model_dump 区分「缺席」与「显式 null」：api_key 显式 null = 清除
    provided = payload.model_dump(exclude_unset=True)
    row = await provider_service.update_provider(
        db,
        user_id,
        provider_id,
        name=provided.get("name"),
        protocol=provided.get("protocol"),
        base_url=provided.get("base_url"),
        model_id=provided.get("model_id"),
        is_default=provided.get("is_default"),
        api_key=provided.get("api_key"),
        api_key_provided="api_key" in provided,
        settings=settings,
    )
    return {"data": provider_service.provider_payload(row)}


@router.delete("/{provider_id}")
async def delete_provider(
    provider_id: int,
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    await provider_service.delete_provider(db, user_id, provider_id)
    return {"data": {"id": provider_id, "deleted": True}}
