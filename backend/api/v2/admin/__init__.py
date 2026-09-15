"""V2 admin 路由包（Phase 7）：聚合 router 由 main.py 以 prefix="/api/admin" 挂载。

纪律：
- 子模块路由一律声明**相对路径**（/invitations 等）——最终路径 = 挂载前缀 +
  相对路径，子模块不得硬编码 /api/admin 前缀（路由可移植性 + 挂载点单点）；
- 门依赖（get_v2_admin_auth / require_admin_reason / admin_idem_begin /
  admin_idem_store）统一定义于 ``_deps`` 并由此处 re-export，路由模块从本包
  导入，避免双导入路径（backend/api/v2/__init__.py 同款纪律）。
"""

from fastapi import APIRouter

from backend.api.v2.admin._deps import (
    admin_idem_begin,
    admin_idem_store,
    get_v2_admin_auth,
    require_admin_reason,
)
from backend.api.v2.admin.invitations import router as invitations_router
from backend.api.v2.admin.users import router as users_router

admin_api_router = APIRouter()

# 子域路由随 Phase 7 任务逐个 include（T2 invitations、T3a users 已挂；T3b suspend/
# unsuspend 归 users 模块追加；T4 reviews、T5 reports/ban、T6 工具与 kill-switch、
# T7 receipts、T8 审计随任务追加）。
# _deps 先于子模块导入：路由模块经本包取门依赖（见模块 docstring 纪律）。
admin_api_router.include_router(invitations_router)
admin_api_router.include_router(users_router)

__all__ = [
    "admin_api_router",
    "admin_idem_begin",
    "admin_idem_store",
    "get_v2_admin_auth",
    "require_admin_reason",
]
