# tests/test_config_security.py
import base64
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config import Settings, get_settings


def _base_env(**over):
    env = {
        "SECRET_KEY": "x" * 32,
        "TASK_TOKEN_SECRET": "t" * 32,
        "ALLOW_INSECURE_SECRETS": "false",
    }
    env.update(over)
    return env


def test_rejects_default_secret_key():
    with pytest.raises(ValidationError):
        Settings(**_base_env(SECRET_KEY="replace-me"))


def test_rejects_placeholder_secret_key():
    with pytest.raises(ValidationError):
        Settings(**_base_env(SECRET_KEY="your-secret-key-here"))


def test_rejects_empty_task_token_secret():
    with pytest.raises(ValidationError):
        Settings(**_base_env(TASK_TOKEN_SECRET=""))


def test_rejects_shared_secret_between_web_and_task_token():
    with pytest.raises(ValidationError):
        Settings(**_base_env(SECRET_KEY="s" * 32, TASK_TOKEN_SECRET="s" * 32))


def test_rejects_change_me_prefix_secret_key():
    """`.env.example` 类 change-me- 占位符必须被拒绝（防模板直启）。"""
    with pytest.raises(ValidationError):
        Settings(**_base_env(SECRET_KEY="change-me-" + "x" * 32))


def test_rejects_change_me_prefix_task_token_secret():
    with pytest.raises(ValidationError):
        Settings(**_base_env(TASK_TOKEN_SECRET="change-me-" + "t" * 32))


def test_rejects_31_char_secret_key():
    with pytest.raises(ValidationError):
        Settings(**_base_env(SECRET_KEY="x" * 31))


def test_rejects_31_char_task_token_secret():
    with pytest.raises(ValidationError):
        Settings(**_base_env(TASK_TOKEN_SECRET="t" * 31))


def test_env_example_placeholders_are_rejected():
    """模板钉死：`.env.example` 的 SECRET_KEY/TASK_TOKEN_SECRET 占位值
    构造 Settings（逃生舱关闭）必须校验失败——模板未经编辑不可启动。"""
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    placeholders: dict[str, str] = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("SECRET_KEY="):
            placeholders["SECRET_KEY"] = line.split("=", 1)[1]
        elif line.startswith("TASK_TOKEN_SECRET="):
            placeholders["TASK_TOKEN_SECRET"] = line.split("=", 1)[1]
    assert placeholders.get("SECRET_KEY"), ".env.example 缺少 SECRET_KEY"
    assert placeholders.get("TASK_TOKEN_SECRET"), ".env.example 缺少 TASK_TOKEN_SECRET"
    with pytest.raises(ValidationError):
        Settings(**placeholders, ALLOW_INSECURE_SECRETS="false")


def test_accepts_distinct_secrets():
    s = Settings(**_base_env())
    assert s.TASK_TOKEN_SECRET == "t" * 32
    assert s.ALLOW_INSECURE_SECRETS is False


def test_escape_hatch_allows_insecure_for_dev():
    s = Settings(**_base_env(SECRET_KEY="replace-me", ALLOW_INSECURE_SECRETS="true"))
    assert s.ALLOW_INSECURE_SECRETS is True


def test_get_settings_weak_secret_raises_sanitized_runtime_error(monkeypatch):
    """生产实例化路径（get_settings）崩在弱密钥时只透出干净校验消息。

    pydantic 的 ValidationError 被 str()/traceback 打印时会嵌入截断的
    input_value 片段（泄露 TASK_TOKEN_SECRET 尾巴），必须改抛仅含
    校验消息的 RuntimeError。
    """
    monkeypatch.setenv("SECRET_KEY", "replace-me")
    monkeypatch.setenv("TASK_TOKEN_SECRET", "strong-task-token-secret-LEAKTAIL123456")
    monkeypatch.delenv("ALLOW_INSECURE_SECRETS", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        get_settings()
    msg = str(excinfo.value)
    assert "SECRET_KEY 必须设置为强随机值" in msg
    assert "replace-me" not in msg
    assert "LEAKTAIL123456" not in msg


def test_get_settings_strong_secrets_returns_settings(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv("TASK_TOKEN_SECRET", "t" * 32)
    monkeypatch.delenv("ALLOW_INSECURE_SECRETS", raising=False)
    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.SECRET_KEY == "x" * 32
    assert settings.TASK_TOKEN_SECRET == "t" * 32
    assert settings.ALLOW_INSECURE_SECRETS is False


# ---- V2 认证/会话设置：三把新密钥 + V2 DSN（Phase 2, Task 1）----


def _base_kwargs(**over):
    kwargs = dict(
        SECRET_KEY="x" * 40,
        TASK_TOKEN_SECRET="y" * 40,
        ALLOW_INSECURE_SECRETS=False,
        V2_DATABASE_URL="postgresql+asyncpg://agentcraft_app:p@h:5432/db",
        V2_ADMIN_DATABASE_URL="postgresql+asyncpg://agentcraft_admin:p@h:5432/db",
        MFA_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"m" * 32).decode().rstrip("="),
        EMAIL_OUTBOX_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"o" * 32).decode().rstrip("="),
        RATE_LIMIT_HMAC_KEY=base64.urlsafe_b64encode(b"r" * 32).decode().rstrip("="),
    )
    kwargs.update(over)
    return Settings(**kwargs)


def test_v2_mode_requires_three_new_keys():
    with pytest.raises(ValidationError):
        _base_kwargs(MFA_ENCRYPTION_KEY="")


def test_v2_mode_rejects_short_or_shared_keys():
    with pytest.raises(ValidationError):
        _base_kwargs(MFA_ENCRYPTION_KEY=base64.urlsafe_b64encode(b"m" * 16).decode().rstrip("="))
    same = base64.urlsafe_b64encode(b"z" * 32).decode().rstrip("=")
    with pytest.raises(ValidationError):
        _base_kwargs(MFA_ENCRYPTION_KEY=same, EMAIL_OUTBOX_ENCRYPTION_KEY=same)


def test_v1_only_mode_allows_empty_v2_keys():
    s = _base_kwargs(V2_DATABASE_URL="", V2_ADMIN_DATABASE_URL="")
    assert s.SESSION_COOKIE_SECURE is True


# ---- 校验器分支补漏（Task 15 收口，源自 T1 deferred minor）----


def test_v2_mode_rejects_non_b64url_key():
    """非合法 base64url 的 V2 密钥必须被拒绝（binascii.Error 分支：

    丢弃非字母表字符后剩 5 个数据字符，%4==1 触发 Invalid padding）。
    """
    with pytest.raises(ValidationError):
        _base_kwargs(MFA_ENCRYPTION_KEY="abcde")


def test_v2_mode_rejects_key_shared_with_secret_key():
    """V2 密钥与 SECRET_KEY 同值必须被拒绝。

    共用值本身必须是合法 b64url 32 字节才能穿透 decode/长度检查、
    命中「不得与既有密钥共用」分支（SECRET_KEY 无格式约束，
    43 字符 b64url 同样满足其 ≥32 字符下限）。
    """
    shared = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
    assert len(shared) >= 32
    with pytest.raises(ValidationError):
        _base_kwargs(SECRET_KEY=shared, MFA_ENCRYPTION_KEY=shared)


def test_v2_mode_rejects_half_configured_dsn_pair():
    """V2 DSN 只配置其一（app 有 admin 缺 / admin 有 app 缺）必须被拒绝。"""
    with pytest.raises(ValidationError):
        _base_kwargs(V2_ADMIN_DATABASE_URL="")
    with pytest.raises(ValidationError):
        _base_kwargs(V2_DATABASE_URL="")


def test_v2_mode_rejects_insecure_session_cookie_without_escape_hatch():
    """SESSION_COOKIE_SECURE=false 而 ALLOW_INSECURE_SECRETS=false 必须被拒绝。"""
    with pytest.raises(ValidationError):
        _base_kwargs(SESSION_COOKIE_SECURE=False, ALLOW_INSECURE_SECRETS=False)
