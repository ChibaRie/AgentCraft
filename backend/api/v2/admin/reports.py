"""admin 举报面路由（Phase 7 T5；Sup §6:144-145）。

队列（GET /api/admin/reports）：status='open' 单表分页，created_at DESC + id DESC
稳定序（T2/T3a/T4 列表同款序）；字段复用 create_report 响应同构 brief（id/
target_type/target_id/status/reason/created_at，report_service.report_brief——
零复制漂移；Phase 8 T7 公开化，消费点不再触私有名）；元数据读免 reason 免幂等
（reviews 队列同款）。

resolve（POST /api/admin/reports/{report_id}/resolve，body ``{action, reason}``——
note→reason 改名 D10-10）为冻结服务薄壳：门序（D6 钉死）CSRF → 认证 →
get_v2_admin_auth 三重门 → reason 门 → 幂等 begin（app 裸会话）→ admin 事务
（resolve_report + admin_idem_store 同事务收口）→ 提交。壳层不重复服务内断言：
400 VALIDATION_ERROR（action 词表外/非 UUID）、404 NOT_FOUND、409
REPORT_ALREADY_RESOLVED / USER_STATUS_CONFLICT 全部透传。

ban_author（D3 级联两段拆分）壳层在提交后追加 post-commit 级联（kill_tool
terminator / T3b suspend 同款序）：admin_user_service.run_suspension_cascade
（经模块属性调用，便于测试故障注入）的 receipts 并入响应；级联失败=有界窗口
（状态已提交不回滚），logger.exception 后照常返回 cascade=null——重试幂等自愈。
store 载荷为提交时刻基线（cascade=null），重放原样返回该基线（重放不重跑级联）。
dismiss/takedown 无级联段（store 即响应，同 unsuspend 形态）。路由一律相对路径
（挂载前缀 /api/admin 由 main.py 单点提供）。
"""

import logging
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from backend.api.v2.admin import (
    admin_idem_begin,
    admin_idem_store,
    get_v2_admin_auth,
    require_admin_reason,
)
from backend.v2 import admin_user_service, idempotency, report_service
from backend.v2.idempotency import require_key_header
from backend.v2.models.content import Report
from backend.v2.models.tasking import TaskMessage
from backend.v2.runtime import V2Runtime, get_v2_runtime
from backend.v2.session_service import V2AuthContext

logger = logging.getLogger("agentcraft.admin.reports")

router = APIRouter()

# 列表分页默认（1≤size≤100；Sup §7 列表信封附 total/page/size）
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20
_LIST_PAGE_MAX = 100


class ResolvePayload(BaseModel):
    """POST resolve 载荷：action 词表校验在服务层（400 VALIDATION_ERROR 透传）；
    reason 缺/空 400 ADMIN_REASON_REQUIRED 走 require_admin_reason，不由 pydantic
    必填（契约信封优先）；extra=forbid 防未知字段静默忽略（T3a/T4 同款）。"""

    model_config = ConfigDict(extra="forbid")

    action: str
    reason: str | None = None


def _parse_uuid_or_400(value: str) -> _uuid.UUID:
    """report_id 路径参数解析（非 UUID → 400 VALIDATION_ERROR，沿契约边界注记）。"""
    try:
        return _uuid.UUID(str(value))
    except ValueError:
        raise _validation("report_id 须为合法 UUID") from None


def _validation(message: str) -> HTTPException:
    # VALIDATION_ERROR 为 V1 约定形状字面（不经 ErrorCode 注册表，同 reviews.py）
    return HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": message})


@router.get("/reports")
async def list_reports(
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    page: int = _DEFAULT_PAGE,
    size: int = _DEFAULT_PAGE_SIZE,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """open 举报队列（单表分页；元数据读免 reason 免幂等）。非法分页 400。"""
    if page < 1 or size < 1 or size > _LIST_PAGE_MAX:
        raise _validation("分页参数非法（page≥1，1≤size≤100）")
    async with runtime.admin_factory() as db:
        total = int(
            (
                await db.execute(
                    select(func.count()).select_from(Report).where(Report.status == "open")
                )
            ).scalar_one()
        )
        rows = (
            (
                await db.execute(
                    select(Report)
                    .where(Report.status == "open")
                    .order_by(Report.created_at.desc(), Report.id.desc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
            )
            .scalars()
            .all()
        )
        items = [report_service.report_brief(row) for row in rows]
    return JSONResponse(
        status_code=200,
        content={"data": {"items": items, "total": total, "page": page, "size": size}},
    )


@router.get("/reports/{report_id}")
async def get_report(
    report_id: str,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """举报详情（Phase 9 T2：Sup §10.11(c)）——元数据读免 reason 免审计（响应仅含
    举报元数据 + 目标定位 id，不含内容）。message 目标额外解析 task_id（消息
    行存在→字符串，已删/不存在→null；非 message 目标→null），去管理员手录任务号。
    非 UUID 路径参数 400 VALIDATION_ERROR；不存在 404 NOT_FOUND。"""
    rid = _parse_uuid_or_400(report_id)
    async with runtime.admin_factory() as db:
        report = (await db.execute(select(Report).where(Report.id == rid))).scalar_one_or_none()
        if report is None:
            raise HTTPException(
                status_code=404, detail={"code": "NOT_FOUND", "message": "举报不存在"}
            )
        payload = report_service.report_brief(report)
        task_id: str | None = None
        if report.target_type == "message":
            task_id = (
                await db.execute(
                    select(TaskMessage.task_id).where(TaskMessage.id == report.target_id)
                )
            ).scalar_one_or_none()
            task_id = str(task_id) if task_id is not None else None
        payload["task_id"] = task_id
    return JSONResponse(status_code=200, content={"data": payload})


@router.post("/reports/{report_id}/resolve")
async def resolve_report(
    report_id: str,
    payload: ResolvePayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """处置举报（200）：admin 事务（resolve_report + store）→ 提交；action=
    ban_author 时提交后 run_suspension_cascade（D3 两段拆分），receipts 并入
    响应。级联失败=有界窗口（状态已提交，不回滚），cascade=null 照常 200。"""
    admin_id = str(ctx.user.id)
    body = payload.model_dump(exclude_unset=True)
    reason = require_admin_reason(body.get("reason"))
    body["reason"] = reason  # 原样参与 request_hash（D11：归一化不在壳层做）
    req_hash = idempotency.request_hash(body)
    replay = await admin_idem_begin(
        runtime, request=request, admin_id=admin_id, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with runtime.admin_factory() as db:
        async with db.begin():
            result = await report_service.resolve_report(
                db,
                report_id=report_id,
                action=payload.action,
                admin_id=admin_id,
                reason=reason,
                request_id=None,
            )
            is_ban = "banned_user_id" in result
            await admin_idem_store(
                db,
                request=request,
                admin_id=admin_id,
                key=idem_key,
                req_hash=req_hash,
                status_code=200,
                payload={"data": {**result, "cascade": None}} if is_ban else {"data": result},
            )
    if is_ban:
        try:
            cascade = await admin_user_service.run_suspension_cascade(
                runtime, target_user_id=result["banned_user_id"], executor=runtime.executor
            )
        except Exception:  # noqa: BLE001 - 有界窗口：状态已提交，级联失败不回滚（D3）
            logger.exception(
                "ban_author 级联失败（有界窗口；重试幂等自愈）：report_id=%s", report_id
            )
            cascade = None
        result = {**result, "cascade": cascade}
    return JSONResponse(status_code=200, content={"data": result})
