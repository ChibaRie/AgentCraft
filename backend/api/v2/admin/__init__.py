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

admin_api_router = APIRouter()

# 子域路由随 Phase 7 任务逐个 include（T2 invitations、T3 users、T4 reviews、
# T5 reports/ban、T6 工具与 kill-switch、T7 receipts、T8 审计）。本任务（T1）
# 仅交付骨架：空 router + 门依赖，挂载后无路由（404 由框架兜底，无行为面）。

__all__ = [
    "admin_api_router",
    "admin_idem_begin",
    "admin_idem_store",
    "get_v2_admin_auth",
    "require_admin_reason",
]
