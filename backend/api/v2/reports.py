"""V2 举报路由（Sup §5:119；Phase 4 T8）。

限流纪律（rate_limit.py 模块 docstring）：enforce 自管事务独立提交，在**任何
业务查询之前**以 app 裸会话调用——429 短路，随后才进幂等/owner 事务。
依赖函数 `_enforce_report_limit` 必须定义在路由装饰器之前（默认参数在装饰器
求值时解析，后置定义会在导入期 NameError）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend.api.v2.content_schemas import ReportCreatePayload
from backend.v2 import idempotency, report_service
from backend.v2.idempotency import require_key_header
from backend.v2.rate_limit import enforce, hmac_subject
from backend.v2.runtime import V2Runtime, get_v2_runtime
from backend.v2.session_service import V2AuthContext, get_v2_auth

router = APIRouter()


async def _enforce_report_limit(
    request_user: V2AuthContext = Depends(get_v2_auth),
    runtime: V2Runtime = Depends(get_v2_runtime),
) -> None:
    """限流先于目标校验（照抄 providers.py test 端点 91-104 的 app 裸会话 enforce 形态）。"""
    async with runtime.app_factory() as db:
        await enforce(
            db, scope="report", subjects=[hmac_subject("user", str(request_user.user.id))]
        )


@router.post("/reports")
async def create_report(
    payload: ReportCreatePayload,
    request_user: V2AuthContext = Depends(get_v2_auth),
    idem_key: str = Depends(require_key_header),
    runtime: V2Runtime = Depends(get_v2_runtime),
    _: None = Depends(_enforce_report_limit),
) -> JSONResponse:
    """提交举报（写幂等；10 次/天/用户；目标矩阵 D10 在服务层）。"""
    body = payload.model_dump()
    outcome = await report_service.create_report(
        runtime,
        reporter_id=str(request_user.user.id),
        payload=body,
        idem_key=idem_key,
        idem_hash=idempotency.request_hash(body),
    )
    if isinstance(outcome, report_service.Replay):
        return JSONResponse(status_code=outcome.status_code, content=outcome.response_json)
    return JSONResponse(status_code=200, content={"data": outcome})
