# tests/test_config_security.py
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
