"""V2 API 包：路由聚合与依赖 re-export。

FastAPI 依赖（get_v2_runtime / get_admin_db / owner_session）统一定义于
``backend.v2.runtime``，此处仅 re-export，避免出现双导入路径。
"""

from fastapi import APIRouter

from backend.api.v2 import account, auth
from backend.v2.runtime import client_ip, get_admin_db, get_v2_runtime, owner_session

v2_api_router = APIRouter()
v2_api_router.include_router(auth.router)
v2_api_router.include_router(account.router)

__all__ = [
    "client_ip",
    "get_admin_db",
    "get_v2_runtime",
    "owner_session",
    "v2_api_router",
]
