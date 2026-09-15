"""admin 目录管理与 kill switch 路由（Phase 7 T6；Sup §6 + D10-3/4/11）。

- GET /catalog/tools、GET /catalog/providers：目录元数据读（免 reason 免幂等），
  全量视图含停用条目（PUT 目标渲染所需，D10-11）；
- PUT /catalog/tools：body ``{tool_id, version, enabled, reason}`` =
  set_tool_enabled 薄壳（D10-4 载荷钉死；审计 tool_catalog.set_enabled 由冻结
  服务内写）；
- PUT /catalog/providers/{provider_id}：body ``{enabled?, models?, reason}``——
  启停/白名单调整，审计 catalog.provider.update（before/after）；
- POST /tools/{tool_id}/kill-switch：body ``{version, reason}``（D10-3 单版本
  粒度）——**D1 例外申报壳**：不复用 kill_tool（其自持事务与幂等 store 同事务
  矛盾），壳内复刻编排体：admin 门 → 幂等 begin（app 裸会话）→ admin 事务内
  ``set_tool_enabled + admin_idem_store`` → 提交 → ``build_terminator(runtime,
  runtime.executor)(tool_id, version)``（executor None 跳过 terminator = 只停用
  不联动降级）。receipt["termination"] 后置并入响应（store 基线
  termination=None——重放不重跑 terminator，users.py suspend cascade 同款）；
  already_in_state 短路不写审计但 terminator 仍执行（回执空集幂等）。

门序（D6 钉死）：CSRF → 认证 → admin 三重门 → 幂等 begin → 限流（仅
kill-switch）→ 状态门 → 服务。kill-switch 限流挂接在幂等 begin **之后**（重放
不耗窗）——以模块级助手在壳体内显式调用而非路由 Depends（依赖在 handler 前
统一求值，会把限流提前到幂等之前；无 Depends 默认参数，无 reports.py:22-30
「定义先于装饰器」的导入期求值问题，仍按范本将助手定义置于路由之前）。路由
一律相对路径（挂载前缀 /api/admin 由 main.py 单点提供）。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.api.v2.admin import (
    admin_idem_begin,
    admin_idem_store,
    get_v2_admin_auth,
    require_admin_reason,
)
from backend.v2 import admin_catalog_service, idempotency
from backend.v2.idempotency import require_key_header
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, get_v2_runtime
from backend.v2.session_service import V2AuthContext
from backend.v2.task_executor import build_terminator
from backend.v2.tool_service import set_tool_enabled

router = APIRouter()


class ToolEnabledPayload(BaseModel):
    """PUT /catalog/tools 载荷（D10-4 钉死三字段 + reason；extra=forbid）。"""

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    version: str
    enabled: bool
    reason: str | None = None


class ProviderCatalogUpdatePayload(BaseModel):
    """PUT /catalog/providers/{id} 载荷：启停/白名单可选（None = 不变）。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    models: list[str] | None = None
    reason: str | None = None


class KillSwitchPayload(BaseModel):
    """POST /tools/{tool_id}/kill-switch 载荷（D10-3 单版本粒度）。"""

    model_config = ConfigDict(extra="forbid")

    version: str
    reason: str | None = None


async def _enforce_kill_switch_limit(runtime: V2Runtime, admin_id: str) -> None:
    """admin_kill_switch 限流（Sup:223：10 次/小时/用户；T1 登记 scope）。

    enforce 自管事务独立提交（rate_limit.py 事务契约），与后续 admin 事务解耦。
    """
    async with runtime.app_factory() as db:
        await enforce(db, scope="admin_kill_switch", subjects=[hmac_subject("user", admin_id)])


@router.get("/catalog/tools")
async def list_tools(
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """工具目录全量视图（元数据读免 reason；含停用条目）。"""
    async with runtime.admin_factory() as db:
        result = await admin_catalog_service.list_tools(db)
    return JSONResponse(status_code=200, content={"data": result})


@router.put("/catalog/tools")
async def update_tool(
    payload: ToolEnabledPayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """工具启停（set_tool_enabled 薄壳；幂等三段壳，审计由服务内写）。"""
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
            result = await set_tool_enabled(
                db,
                tool_id=payload.tool_id,
                version=payload.version,
                enabled=payload.enabled,
                admin_id=admin_id,
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


@router.get("/catalog/providers")
async def list_providers(
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """Provider 目录全量视图（契约外补充端点，D10-11；含停用条目）。"""
    async with runtime.admin_factory() as db:
        result = await admin_catalog_service.list_providers(db)
    return JSONResponse(status_code=200, content={"data": result})


@router.put("/catalog/providers/{provider_id}")
async def update_provider(
    provider_id: str,
    payload: ProviderCatalogUpdatePayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """Provider 目录启停/白名单调整（幂等三段壳；审计 catalog.provider.update）。"""
    admin_id = str(ctx.user.id)
    body = payload.model_dump(exclude_unset=True)
    reason = require_admin_reason(body.get("reason"))
    body["reason"] = reason
    req_hash = idempotency.request_hash(body)
    replay = await admin_idem_begin(
        runtime, request=request, admin_id=admin_id, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    async with runtime.admin_factory() as db:
        async with db.begin():
            result = await admin_catalog_service.update_provider(
                db,
                admin_id=admin_id,
                provider_id=provider_id,
                enabled=payload.enabled,
                models=payload.models,
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


@router.post("/tools/{tool_id}/kill-switch")
async def kill_switch(
    tool_id: str,
    payload: KillSwitchPayload,
    request: Request,
    ctx: V2AuthContext = Depends(get_v2_admin_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> JSONResponse:
    """kill switch（D1 例外申报壳：编排体复刻，见模块 docstring）。

    admin 事务（set_tool_enabled 停用 + 审计 + store 基线 termination=None）提交
    后接 terminator 联动（queued/running 任务 aborted(tool_revoked) + 运行轮
    bounded stop）；executor None = 只停用不联动降级（termination=None）。
    """
    admin_id = str(ctx.user.id)
    body = payload.model_dump(exclude_unset=True)
    reason = require_admin_reason(body.get("reason"))
    body["reason"] = reason
    req_hash = idempotency.request_hash(body)
    replay = await admin_idem_begin(
        runtime, request=request, admin_id=admin_id, key=idem_key, req_hash=req_hash
    )
    if replay is not None:
        return replay
    # D6 门序：限流在幂等 begin 之后——重放不耗窗
    await _enforce_kill_switch_limit(runtime, admin_id)
    async with runtime.admin_factory() as db:
        async with db.begin():
            result = await set_tool_enabled(
                db,
                tool_id=tool_id,
                version=payload.version,
                enabled=False,
                admin_id=admin_id,
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
                payload={"data": {**result, "termination": None}},
            )
    termination = None
    if runtime.executor is not None:
        termination = await build_terminator(runtime, runtime.executor)(tool_id, payload.version)
    return JSONResponse(status_code=200, content={"data": {**result, "termination": termination}})
