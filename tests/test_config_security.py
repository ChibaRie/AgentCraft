# tests/test_config_security.py
import pytest
from pydantic import ValidationError

from backend.config import Settings


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
