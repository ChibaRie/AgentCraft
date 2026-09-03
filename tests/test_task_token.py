"""任务令牌测试（§7.7 proxy 按令牌路由的认证前提）。"""

import pytest

from backend.services.task_token import TaskTokenInvalid, create_task_token, decode_task_token


def test_roundtrip():
    token = create_task_token(7, "inst-abc", "deepseek-chat")
    claims = decode_task_token(token)
    assert claims == {"task_id": 7, "instance": "inst-abc", "model": "deepseek-chat"}


def test_tampered_token_rejected():
    token = create_task_token(7, "inst-abc", "m")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(TaskTokenInvalid):
        decode_task_token(tampered)


def test_garbage_rejected():
    with pytest.raises(TaskTokenInvalid):
        decode_task_token("not-a-jwt")
    with pytest.raises(TaskTokenInvalid):
        decode_task_token("")


def test_expired_token_rejected():
    token = create_task_token(7, "inst", "m", ttl_hours=-1)
    with pytest.raises(TaskTokenInvalid):
        decode_task_token(token)


def test_tokens_differ_per_instance():
    a = create_task_token(7, "inst-1", "m")
    b = create_task_token(7, "inst-2", "m")
    assert a != b, "容器重建必须产生不同令牌"
