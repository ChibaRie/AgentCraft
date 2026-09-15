"""admin 操作理由门（Phase 7 T4，裁决 D11 收敛）：服务层共享单例。

自 review_service.py:46-51 原样提取（Phase 4 顺延项）：服务层 admin 写操作统一
reason 门——None/非 str/空白 → 400 ADMIN_REASON_REQUIRED；合法值 strip 返回
（审计 reason 归一化在此收口）。公开名 ``require_reason``（原私有名 _reason_gate
随提取消亡，review_service 两处调用点已切换）。

边界澄清（D11）：壳层 admin 写端点使用
``backend.api.v2.admin._deps.require_admin_reason``（独立实现，含 ≤2000 长度门
——服务层无长度门，两者不混用）；本模块是服务层侧唯一事实源，
report_service.py / tool_service.py 的私有副本属冻结面不动（登记 Phase 8）。
"""

from backend.errors import AgentCraftError, ErrorCode


def require_reason(reason: str) -> str:
    """服务层 admin 操作理由门：None/非 str/空白 → 400 ADMIN_REASON_REQUIRED；
    合法值 strip 后返回。"""
    if not isinstance(reason, str) or not reason.strip():
        raise AgentCraftError(
            ErrorCode.ADMIN_REASON_REQUIRED, "管理员操作必须提供 reason", http_status=400
        )
    return reason.strip()
