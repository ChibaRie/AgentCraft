"""V2 认证面路由。

Task 4 仅占位 ``GET /health``：探测路由挂 ``Depends(get_v2_runtime)``，作为
503 语义的探针（V1-only 模式下整个 /api/v2 不可用）；后续任务（T12/T13）
替换为真实端点。依赖统一定义于 ``backend.v2.runtime``，此处仅消费。
"""

from fastapi import APIRouter, Depends

from backend.v2.runtime import V2Runtime, get_v2_runtime

router = APIRouter()


@router.get("/health")
async def v2_health(_runtime: V2Runtime = Depends(get_v2_runtime)) -> dict[str, object]:
    return {"data": {"status": "ok"}}
