"""admin 任务读端点（Phase 8 T4；Sup §10.5 D8/D9）。

门序（D6 钉死）：CSRF → 认证（get_v2_auth）→ get_v2_admin_auth 三重门 → 业务。
读写全受门（Sup:126）。分层（§10.5）：

- 内容读（messages/files）：**reason 走查询参数必带**（D9，沿 admin 产物下载
  #17 先例——None/空白 400 ADMIN_REASON_REQUIRED，>2000 400 VALIDATION_ERROR，
  壳层 require_admin_reason 消费；URL 留痕风险（R7）登记接受）+ **先审计后读**
  （action=task.message.read / task.file_list.read，detail={task_id} 零内容
  材料；flush 失败异常向上 → 端点壳事务回滚拒读；404 预取短路不落审计）；
- 元数据读（快照）：免 reason 免审计——owner 面 D14 视图同形 + §10.3
  expert/provider 展示字段（get_task_view 冻结装配复用）。

路由一律相对路径（挂载前缀 /api/admin 由 main.py 单点提供）。
"""

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from backend.api.v2.admin import get_v2_admin_auth, require_admin_reason
from backend.v2 import admin_audit_service
from backend.v2.runtime import V2Runtime, get_v2_runtime
from backend.v2.session_service import V2AuthContext

router = APIRouter()

# 消息读默认/上限（§10.5：默认 50 上限 200——owner 面 tasks.py messages 同值）
_MESSAGES_DEFAULT_LIMIT = 50
_MESSAGES_MAX_LIMIT = 200


@router.get("/tasks/{task_id}/messages")
async def list_task_messages_admin(
    task_id: str,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    after: int = Query(0, ge=0),
    limit: int = Query(_MESSAGES_DEFAULT_LIMIT, ge=1, le=_MESSAGES_MAX_LIMIT),
    reason: str | None = None,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """admin 消息读（内容读，先审计后读）：reason 查询参数必带 → admin 事务内
    先审计（flush 落定）后 task_views.list_task_messages 冻结复用——
    event_sequence 升序全量正文，?after=<event_seq>&limit= 游标截断。"""
    reason_checked = require_admin_reason(reason)
    async with runtime.admin_factory() as db:
        async with db.begin():
            items = await admin_audit_service.admin_list_task_messages(
                db,
                task_id=task_id,
                after=after,
                limit=limit,
                admin_id=str(ctx.user.id),
                reason=reason_checked,
                request_id=None,
            )
    return JSONResponse(status_code=200, content={"data": items})


@router.get("/tasks/{task_id}/files")
async def list_task_files_admin(
    task_id: str,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    direction: str | None = None,
    reason: str | None = None,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """admin 文件元数据读（内容读，先审计后读）：reason 查询参数必带 →
    admin 事务内先审计（flush 落定）后元数据窄 SELECT——仅
    file_name/sha256/size_bytes/state 四键（无内容字节）；direction 词表
    校验在服务层（词表外/缺失 400 VALIDATION_ERROR，先于审计）。"""
    reason_checked = require_admin_reason(reason)
    async with runtime.admin_factory() as db:
        async with db.begin():
            items = await admin_audit_service.admin_list_task_files(
                db,
                task_id=task_id,
                direction=direction,
                admin_id=str(ctx.user.id),
                reason=reason_checked,
                request_id=None,
            )
    return JSONResponse(status_code=200, content={"data": items})


@router.get("/tasks/{task_id}")
async def get_task_admin(
    task_id: str,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """admin 任务快照（元数据读，免 reason 免审计）：owner 面同形视图 +
    §10.3 expert/provider 展示字段（admin_read 上下文全量可见）；缺失/已删除
    统一 404。"""
    async with runtime.admin_factory() as db:
        view = await admin_audit_service.admin_get_task(
            db, task_id=task_id, admin_id=str(ctx.user.id)
        )
    return JSONResponse(status_code=200, content={"data": view})
