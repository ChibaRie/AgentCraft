"""canonical 哈希纯函数测试（裁决 D8）。"""

import hashlib
import json

import pytest

from backend.v2.content_hash import (
    MAX_CONTENT_BYTES,
    ContentTooLarge,
    assert_content_size,
    canonical_json,
    content_sha256,
)


def test_canonical_is_key_order_independent_and_compact():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert canonical_json({"a": 1}) == '{"a":1}'  # 紧凑分隔符，无空格


def test_canonical_preserves_unicode():
    assert "专家" in canonical_json({"name": "专家"})  # ensure_ascii=False


def test_content_sha256_matches_manual_computation():
    content = {"name": "x", "persona": "你好"}
    expect = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    assert content_sha256(content) == expect
    assert len(content_sha256(content)) == 64


def test_assert_content_size_rejects_oversize():
    big = {"persona": "字" * (MAX_CONTENT_BYTES)}  # 单字段即超限（UTF-8 中文 3B/字）
    with pytest.raises(ContentTooLarge):
        assert_content_size(big)
    assert_content_size({"persona": "ok"})  # 正常内容不抛
