"""平台工具目录服务（Phase 5 裁决 D5）：启停原语 / kill switch 编排 / 泛化校验。

授权面（0001:1018,1023-1025）：tool_catalog 对 app role 仅 SELECT、audit_logs
对 app 零授权——启停+审计必须整体跑 admin 引擎（与 Phase 4 approve 同构）。
两层形态：
- set_tool_enabled：行级原语，D13 镜像（调用方已 begin 的 admin 会话）；
  with_for_update 行锁 + 幂等短路（已处目标态直接返回、不写审计）。
- kill_tool：编排层（自持 admin 事务）——内部 set_tool_enabled，事务提交后调
  terminator(tool_id, version) 聚合回执；terminator 注入式（Phase 5 测试假件 /
  Phase 6 任务域联动 / Phase 7 HTTP 壳），None = 只停用不联动。queued→aborted
  与 5 秒停止真件归 Phase 6（V1 引擎无 bounded-stop；tasks 表 admin UPDATE
  会被 RLS 静默 0 行——两段式联动随 Phase 6 设计）。
审计红线：detail 仅 tool_id/version/enabled_before/enabled_after；不落
permissions JSONB 整包/任务消息/任何令牌与 Key 材料。
"""

import uuid as _uuid
from collections.abc import Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.models.content import AuditLog, ToolCatalog
from backend.v2.runtime import V2Runtime

KillTerminator = Callable[[str, str], Awaitable[dict]]


def _reason_gate(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise AgentCraftError(
            ErrorCode.ADMIN_REASON_REQUIRED, "管理员操作必须提供 reason", http_status=400
        )
    return reason.strip()


async def set_tool_enabled(
    admin_db: AsyncSession,
    *,
    tool_id: str,
    version: str,
    enabled: bool,
    admin_id: str,
    reason: str,
    request_id: str | None,
) -> dict:
    """行级启停原语（D13 镜像）：幂等短路 + 实际翻转同事务审计。"""
    admin_id = str(admin_id)
    reason = _reason_gate(reason)
    row = (
        await admin_db.execute(
            select(ToolCatalog)
            .where(ToolCatalog.tool_id == tool_id, ToolCatalog.version == version)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404, detail={"code": "NOT_FOUND", "message": "工具目录行不存在"}
        )
    before = row.enabled
    if before == enabled:
        return {
            "tool_id": tool_id,
            "version": version,
            "enabled": enabled,
            "already_in_state": True,
        }
    row.enabled = enabled
    admin_db.add(
        AuditLog(
            actor_id=_uuid.UUID(admin_id),
            action="tool_catalog.set_enabled",
            target_type="tool_catalog",
            target_id=row.id,
            reason=reason,
            request_id=request_id,
            detail={
                "tool_id": tool_id,
                "version": version,
                "enabled_before": before,
                "enabled_after": enabled,
            },
        )
    )
    await admin_db.flush()
    return {"tool_id": tool_id, "version": version, "enabled": enabled, "already_in_state": False}


async def kill_tool(
    runtime: V2Runtime,
    *,
    tool_id: str,
    version: str,
    admin_id: str,
    reason: str,
    request_id: str | None,
    terminator: KillTerminator | None = None,
) -> dict:
    """编排层：自持 admin 事务停用+审计，提交后调 terminator 聚合回执。"""
    admin_id = str(admin_id)
    async with runtime.admin_factory() as db:
        async with db.begin():
            out = await set_tool_enabled(
                db,
                tool_id=tool_id,
                version=version,
                enabled=False,
                admin_id=admin_id,
                reason=reason,
                request_id=request_id,
            )
    receipt = dict(out)
    if terminator is not None:
        receipt["termination"] = await terminator(tool_id, version)
    return receipt


async def assert_tool_enabled(
    db: AsyncSession, tool_id: str, version: str, *, http_status: int = 403
) -> None:
    """泛化校验（app 会话可跑——tool_catalog app 只读）：不存在/停用统一抛。

    消费点：/internal/harness/check-code-style 回调（Phase 5 T4）、Phase 6
    任务创建断言与文件工具执行器。approve 断言（review_service）已有同语义
    实现（409 载体），维持不动。
    """
    enabled = (
        await db.execute(
            select(ToolCatalog.enabled).where(
                ToolCatalog.tool_id == tool_id, ToolCatalog.version == version
            )
        )
    ).scalar_one_or_none()
    if enabled is not True:
        raise AgentCraftError(ErrorCode.TOOL_REVOKED, "工具已停用或不存在", http_status=http_status)
