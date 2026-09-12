"""V2 API 包：路由聚合与依赖 re-export。

FastAPI 依赖（get_v2_runtime / get_admin_db / owner_session / client_ip）统一定义于
``backend.v2.runtime``，认证依赖 get_v2_auth 定义于 ``backend.v2.session_service``；
此处仅 re-export（含 client_ip 便捷 re-export），避免出现双导入路径。
"""

from fastapi import APIRouter

from backend.api.v2 import account, auth, authoring, discover, providers, reports
from backend.v2.runtime import client_ip, get_admin_db, get_v2_runtime, owner_session
from backend.v2.session_service import get_v2_auth

v2_api_router = APIRouter()
v2_api_router.include_router(auth.router)
v2_api_router.include_router(account.router)
v2_api_router.include_router(providers.router)
v2_api_router.include_router(authoring.router)
v2_api_router.include_router(reports.router)
v2_api_router.include_router(discover.router)

__all__ = [
    "client_ip",
    "get_admin_db",
    "get_v2_auth",
    "get_v2_runtime",
    "owner_session",
    "v2_api_router",
]
