"""8 态任务状态机原语（Phase 6 T2）——合法迁移表的唯一事实源。

合法迁移表（aborted 出边 reason=词表全集；reason 是数据附言，状态对裁决，
括号为典型触发方）::

    uploading → queued(commit) | failed(upload_expired | provider_key_revoked) | deleted
    queued    → running(dispatch) | completed(complete) | aborted(user_cancel|tool_revoked)
                | failed(provider_key_revoked) | deleted
    running   → ready(settle) | completed(complete,经 pending_terminal)
                | aborted(user_cancel|tool_revoked|provider_key_revoked)
                | failed(round_failed) | deleted
    ready     → queued(messages) | completed(complete) | aborted(user_cancel|tool_revoked)
                | failed(provider_key_revoked) | deleted
    completed/failed/aborted → deleted
    deleted   → 终态，无出边

assert_transition 仅用于用户请求驱动的状态机入口（Phase 6 D4）：系统路径
（kill switch 联动/executor/reclaim/清扫）一律条件 UPDATE + rowcount 判定，
不走本断言。PENDING_TERMINAL_VALUES 同时是 tasks.pending_terminal 的 CHECK
词表（迁移 0008）与 tasking 模型的 check_enum 值源。
"""

from backend.errors import AgentCraftError, ErrorCode

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "uploading": frozenset({"queued", "failed", "deleted"}),
    "queued": frozenset({"running", "completed", "aborted", "failed", "deleted"}),
    "running": frozenset({"ready", "completed", "aborted", "failed", "deleted"}),
    "ready": frozenset({"queued", "completed", "aborted", "failed", "deleted"}),
    "completed": frozenset({"deleted"}),
    "failed": frozenset({"deleted"}),
    "aborted": frozenset({"deleted"}),
    "deleted": frozenset(),  # 终态，无出边
}

# abort_reason 词表（D7e）；admin_suspended 为 Phase 7 预留登记不接线
ABORT_REASONS: frozenset[str] = frozenset(
    {
        "user_cancel",
        "round_failed",
        "upload_expired",
        "provider_key_revoked",
        "tool_revoked",
        "admin_suspended",
    }
)

# 终态意图位词表（D19）：tasks.pending_terminal 的 CHECK 词表（0008）与模型 check_enum 值源
PENDING_TERMINAL_VALUES = ("completed", "aborted", "deleted")


def assert_transition(cur: str, nxt: str) -> None:
    """用户请求入口的状态机断言：非法迁移抛 TASK_INVALID_TRANSITION(409)。

    仅校验状态对；aborted 边的 reason 合法性由 validate_abort_reason 单独把关
    （reason 是数据附言，不参与状态对裁决）。
    """
    if nxt not in VALID_TRANSITIONS.get(cur, frozenset()):
        raise AgentCraftError(
            ErrorCode.TASK_INVALID_TRANSITION,
            f"任务状态不允许从 {cur} 迁移到 {nxt}",
            http_status=409,
        )


def validate_abort_reason(reason: str) -> None:
    """abort_reason 词表校验：词表外抛 TASK_INVALID_TRANSITION(409)。"""
    if reason not in ABORT_REASONS:
        raise AgentCraftError(
            ErrorCode.TASK_INVALID_TRANSITION,
            f"非法 abort_reason，允许词表：{sorted(ABORT_REASONS)}",
            http_status=409,
        )
