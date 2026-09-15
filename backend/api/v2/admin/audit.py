"""admin 审计查询与产物下载路由（Phase 7 T7；Sup §6:146 + D7h/D10-11）。

门序（D6 钉死）：CSRF → 认证（get_v2_auth）→ get_v2_admin_auth 三重门 → 业务。
读写全受门（Sup:126）。

- GET /audit-logs：六维过滤（actor_id/action/target_type/target_id/since/until）
  全可选 + created_at DESC 分页；元数据读免 reason 免幂等（计划 T7）；信封
  {data: {items,total,page,size}}（Sup §7 列表形状，T3a/T4 同款）。非法过滤/
  分页 400 VALIDATION_ERROR 在服务层统一（admin_user_service.list_users 同构）。
- GET /tasks/{task_id}/artifacts/{file_id}/download：admin 产物下载（D7h）。
  **reason 走查询参数必带**（None/空白 400 ADMIN_REASON_REQUIRED；>2000 400
  VALIDATION_ERROR，壳层 require_admin_reason 消费）；GET 读端点无幂等键
  （Sup §6 Idempotency-Key 为写端点义务；读审计行随 admin 事务落库）。服务
  admin_download_artifact 在 admin_factory().begin() 事务内先写审计（flush
  落定）后复用冻结 resolve_download——审计失败异常向上整事务回滚（拒读）；
  FileResponse 形态同 T8a owner 下载路由（artifacts 登记副本 +
  download_headers attachment/nosniff）。路由一律相对路径（挂载前缀 /api/admin
  由 main.py 单点提供）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse

from backend.api.v2.admin import get_v2_admin_auth, require_admin_reason
from backend.v2 import admin_audit_service
from backend.v2.runtime import V2Runtime, get_v2_runtime
from backend.v2.session_service import V2AuthContext
from backend.v2.task_artifacts import download_headers

router = APIRouter()

# 列表分页默认（服务层校验 1≤size≤100；Sup §7 列表信封附 total/page/size）
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20


@router.get("/audit-logs")
async def list_audit_logs(
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    actor_id: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    page: int = _DEFAULT_PAGE,
    size: int = _DEFAULT_PAGE_SIZE,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """审计查询（Sup:146；六维过滤全可选，created_at DESC 分页；只读免 reason）。"""
    async with runtime.admin_factory() as db:
        result = await admin_audit_service.query_audit_logs(
            db,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            since=since,
            until=until,
            page=page,
            size=size,
        )
    return JSONResponse(status_code=200, content={"data": result})


@router.get("/tasks/{task_id}/artifacts/{file_id}/download")
async def download_task_artifact_admin(
    task_id: str,
    file_id: str,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    reason: str | None = None,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> FileResponse:
    """admin 产物下载（D7h，读审计变体）：reason 查询参数必带 → admin 事务内
    先审计（flush）后 resolve_download → FileResponse（attachment + nosniff）。
    admin 面语义：可见全部用户的存活产物（owner_id 仅解析不过滤）；404/400
    统一信封透传（TASK_NOT_FOUND/FILE_NOT_FOUND/VALIDATION_ERROR）。"""
    reason_checked = require_admin_reason(reason)
    async with runtime.admin_factory() as db:
        async with db.begin():
            path, meta = await admin_audit_service.admin_download_artifact(
                db,
                runtime.storage,
                admin_id=str(ctx.user.id),
                task_id=task_id,
                file_id=file_id,
                reason=reason_checked,
                request_id=None,
            )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        headers=download_headers(meta["file_name"]),
    )
