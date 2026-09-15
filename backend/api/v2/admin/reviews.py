"""admin 审核面路由（Phase 7 T4；Sup §6:127-128）。

队列（GET /api/admin/reviews）：pending_review 的 expert+skill revision 双域汇流，
created_at DESC + id DESC 稳定序合并分页；字段钉死（id/target_type/revision_no/
owner_id/content_json 全量/content_sha256/auto_check/created_at）。content_json
全量出队为 §6:128 审核队列豁免读审计形态（免 reason 免幂等，零读审计行）；
auto_check 读取时对 content_json 重算 ``run_auto_check`` 纯函数（author_service
提交闸同款分派，按 target_type 走 expert/skill 形态），结果不持久化。

approve/reject（POST /api/admin/reviews/{revision_id}/approve|reject，body
``{target_type, reason}``——路径无 target_type，从 body 取）为冻结服务薄壳：
门序（D6 钉死）CSRF → 认证 → get_v2_admin_auth 三重门 → reason 门（壳层
require_admin_reason，含 ≤2000 门）→ 幂等 begin（app 裸会话）→ admin 事务
（壳层 target_type 词表校验（词表外 400，先于服务——服务内 ValueError 不外泄）
→ 调冻结服务 → admin_idem_store 同事务收口）→ 提交。壳层不重复服务内断言：
统一 404 NOT_FOUND（HTTPException.detail）、409 REVIEW_PENDING、非 UUID 路径
参数 400 VALIDATION_ERROR 全部透传。reason 原样参与 request_hash（同 key 异
reason → 409 IDEMPOTENCY_CONFLICT）。路由一律相对路径（挂载前缀 /api/admin 由
main.py 单点提供）。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from backend.api.v2.admin import (
    admin_idem_begin,
    admin_idem_store,
    get_v2_admin_auth,
    require_admin_reason,
)
from backend.v2 import idempotency, review_service
from backend.v2.content_autocheck import run_auto_check
from backend.v2.idempotency import require_key_header
from backend.v2.review_service import _REVIEW_DOMAINS
from backend.v2.runtime import V2Runtime, get_v2_runtime
from backend.v2.session_service import V2AuthContext

router = APIRouter()

# 列表分页默认（1≤size≤100；Sup §7 列表信封附 total/page/size）
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20
_LIST_PAGE_MAX = 100


class ReviewActionPayload(BaseModel):
    """POST approve/reject 载荷：target_type 词表校验在 admin 事务内壳层做（计划
    钉死序：门→幂等 begin→admin 事务→词表校验→服务）；reason 缺/空 400
    ADMIN_REASON_REQUIRED 走 require_admin_reason，不由 pydantic 必填（契约信封
    优先）；extra=forbid 防未知字段静默忽略（T3a 同款）。"""

    model_config = ConfigDict(extra="forbid")

    target_type: str
    reason: str | None = None


def _validation(message: str) -> HTTPException:
    # VALIDATION_ERROR 为 V1 约定形状字面（不经 ErrorCode 注册表，同 author_service/
    # admin_invitation_service）
    return HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": message})


def _queue_brief(revision, target_type: str) -> dict:
    """revision 行 → 队列项（字段钉死；auto_check 读取时重算，不持久化）。"""
    return {
        "id": str(revision.id),
        "target_type": target_type,
        "revision_no": revision.revision_no,
        "owner_id": str(revision.owner_id),
        "content_json": revision.content_json,
        "content_sha256": revision.content_sha256,
        "auto_check": run_auto_check(target_type, revision.content_json),
        "created_at": revision.created_at.isoformat(),
    }


@router.get("/reviews")
async def list_reviews(
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    target_type: str | None = None,
    page: int = _DEFAULT_PAGE,
    size: int = _DEFAULT_PAGE_SIZE,
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """pending_review 审核队列（双域汇流 + target_type 过滤 + 分页；元数据+内容
    读免 reason 免幂等，§6:128 豁免读审计）。词表外 filter 与非法分页 400。"""
    if page < 1 or size < 1 or size > _LIST_PAGE_MAX:
        raise _validation("分页参数非法（page≥1，1≤size≤100）")
    if target_type is not None and target_type not in _REVIEW_DOMAINS:
        raise _validation("target_type 仅支持 expert_revision/skill_revision")
    async with runtime.admin_factory() as db:
        # 双域汇流：pending_review 队列天然小（审核吞吐有界），Python 侧合并后
        # 统一稳定序（created_at DESC + id DESC，T2/T3a 列表同款序）再切页
        rows: list[tuple[object, str]] = []
        scoped = (
            _REVIEW_DOMAINS.items()
            if target_type is None
            else ((target_type, _REVIEW_DOMAINS[target_type]),)
        )
        for dom_type, dom in scoped:
            revisions = (
                (
                    await db.execute(
                        select(dom.revision).where(dom.revision.status == "pending_review")
                    )
                )
                .scalars()
                .all()
            )
            rows.extend((revision, dom_type) for revision in revisions)
        rows.sort(key=lambda pair: (pair[0].created_at, pair[0].id), reverse=True)
        total = len(rows)
        start = (page - 1) * size
        window = rows[start : start + size]
        items = [_queue_brief(revision, dom_type) for revision, dom_type in window]
    return JSONResponse(
        status_code=200,
        content={"data": {"items": items, "total": total, "page": page, "size": size}},
    )


@router.post("/reviews/{revision_id}/approve")
async def approve_review(
    revision_id: str,
    payload: ReviewActionPayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """发布准予（200）：admin 事务内壳层词表校验 → 冻结 approve_revision（锁序/
    CAS/审计语义全在服务）→ store 同事务收口；404/409/400 服务内断言透传。"""
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
            if payload.target_type not in _REVIEW_DOMAINS:
                raise _validation("target_type 仅支持 expert_revision/skill_revision")
            result = await review_service.approve_revision(
                db,
                target_type=payload.target_type,
                revision_id=revision_id,
                reviewer_id=admin_id,
                reason=reason,
                request_id=None,
            )
            await admin_idem_store(
                db,
                request=request,
                admin_id=admin_id,
                key=idem_key,
                req_hash=req_hash,
                status_code=200,
                payload={"data": result},
            )
    return JSONResponse(status_code=200, content={"data": result})


@router.post("/reviews/{revision_id}/reject")
async def reject_review(
    revision_id: str,
    payload: ReviewActionPayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """驳回（200）：approve 同构薄壳（冻结 reject_revision；实体与指针不被触碰）。"""
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
            if payload.target_type not in _REVIEW_DOMAINS:
                raise _validation("target_type 仅支持 expert_revision/skill_revision")
            result = await review_service.reject_revision(
                db,
                target_type=payload.target_type,
                revision_id=revision_id,
                reviewer_id=admin_id,
                reason=reason,
                request_id=None,
            )
            await admin_idem_store(
                db,
                request=request,
                admin_id=admin_id,
                key=idem_key,
                req_hash=req_hash,
                status_code=200,
                payload={"data": result},
            )
    return JSONResponse(status_code=200, content={"data": result})
