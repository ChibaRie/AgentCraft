"""8 态任务状态机原语单元测试（Phase 6 T2）。

非法迁移四钉例（brief 清单）+ 全表正扫 + 终态簇/词表/意图位词表契约。
纯函数无 DB 依赖；错误类型按 backend.errors 惯例（AgentCraftError + 注册码）。
"""

import pytest

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.task_state import (
    ABORT_REASONS,
    PENDING_TERMINAL_VALUES,
    VALID_TRANSITIONS,
    assert_transition,
    validate_abort_reason,
)

# brief 钉死的四条非法迁移
ILLEGAL_TRANSITIONS = [
    ("uploading", "running"),
    ("ready", "running"),
    ("completed", "ready"),
    ("deleted", "queued"),
]


@pytest.mark.parametrize(("cur", "nxt"), ILLEGAL_TRANSITIONS)
def test_illegal_transition_rejected_with_409(cur: str, nxt: str) -> None:
    with pytest.raises(AgentCraftError) as excinfo:
        assert_transition(cur, nxt)
    err = excinfo.value
    assert err.code is ErrorCode.TASK_INVALID_TRANSITION
    assert err.http_status == 409


def test_every_declared_transition_accepted() -> None:
    """迁移表自洽正扫：每条 (cur, nxt) 都被 assert_transition 放行（不抛）。"""
    for cur, targets in VALID_TRANSITIONS.items():
        for nxt in targets:
            assert_transition(cur, nxt)


def test_transition_table_shape() -> None:
    """8 态全键；deleted 终态无出边；completed/failed/aborted 唯一出边是 deleted。"""
    assert set(VALID_TRANSITIONS) == {
        "uploading",
        "queued",
        "running",
        "ready",
        "completed",
        "failed",
        "aborted",
        "deleted",
    }
    assert VALID_TRANSITIONS["deleted"] == frozenset()
    for terminal in ("completed", "failed", "aborted"):
        assert VALID_TRANSITIONS[terminal] == frozenset({"deleted"})


def test_unknown_current_state_rejected() -> None:
    with pytest.raises(AgentCraftError) as excinfo:
        assert_transition("bogus", "queued")
    assert excinfo.value.code is ErrorCode.TASK_INVALID_TRANSITION


@pytest.mark.parametrize(
    "reason",
    ["", "ACCOUNT_DELETED", "User_Cancel", "user cancel", "hacker", "admin_suspend"],
)
def test_abort_reason_outside_lexicon_rejected(reason: str) -> None:
    with pytest.raises(AgentCraftError) as excinfo:
        validate_abort_reason(reason)
    assert excinfo.value.code is ErrorCode.TASK_INVALID_TRANSITION
    assert excinfo.value.http_status == 409


def test_abort_reason_lexicon_exact() -> None:
    """词表六值（D7e）：account_deleted 已随 D18 物理删除移除；admin_suspended 为
    Phase 7 预留登记不接线——登记但不允许词表外扩。"""
    assert ABORT_REASONS == frozenset(
        {
            "user_cancel",
            "round_failed",
            "upload_expired",
            "provider_key_revoked",
            "tool_revoked",
            "admin_suspended",
        }
    )
    for reason in ABORT_REASONS:
        validate_abort_reason(reason)  # 全集放行（不抛）


def test_pending_terminal_values_shape() -> None:
    assert PENDING_TERMINAL_VALUES == ("completed", "aborted", "deleted")
