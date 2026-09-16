from fastapi import APIRouter

from backend.api import internal

# V1 应用面已随 Phase 8 T13 cutover 物理删除（D2 全量裁决）；本包仅存
# internal_router（/internal 平台工具回调 + provider-grant，D15-Option1 冻结面）。
# 公开 API 面由 backend.api.v2 承载（main.py 以契约路径前缀 /api 挂载）。
internal_router = APIRouter()
internal_router.include_router(internal.router)
