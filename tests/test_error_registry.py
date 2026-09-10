# tests/test_error_registry.py
from backend.errors import AgentCraftError, ErrorCode


def test_new_error_codes_registered():
    for code in (
        "INVITATION_INVALID",
        "IDEMPOTENCY_CONFLICT",
        "INPUT_COMMITTED",
        "PROVIDER_NOT_CONFIGURED",
        "KEY_VERSION_REVOKED",
        "TOOL_REVOKED",
        "ADMIN_REASON_REQUIRED",
        "ADMIN_MFA_REQUIRED",
        "QUOTA_DAILY_EXCEEDED",
        "REVISION_NOT_PUBLISHED",
    ):
        assert ErrorCode(code)


def test_error_carries_code_status_and_message():
    err = AgentCraftError(ErrorCode.INPUT_COMMITTED, "输入已冻结", http_status=409)
    assert err.code is ErrorCode.INPUT_COMMITTED
    assert err.http_status == 409
    assert "冻结" in str(err)


def test_no_legacy_codes_removed():
    # V1 既有错误码仍可表达（保留验证）
    assert ErrorCode("PROMPT_TOO_LARGE")
