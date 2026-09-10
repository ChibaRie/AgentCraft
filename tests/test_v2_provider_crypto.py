"""provider_crypto 单元测试：零 DB；KEK 经注入替换（kek_source 参数）。
红线：任何路径（含失败）不得产生携带 key 材料的日志记录（caplog 断言）。"""

import base64
import json
import logging

import pytest

from backend.utils.crypto import EncryptionError, make_keyring
from backend.v2 import provider_crypto
from backend.v2.provider_crypto import KeySealer, provider_dek_aad, provider_key_aad

_KEK = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
_KEY = "sk-test-abcdef1234567890"
_PID_A = "0197aaaa-7aaa-7aaa-7aaa-aaaaaaaaaaaa"
_PID_B = "0197bbbb-7bbb-7bbb-7bbb-bbbbbbbbbbbb"


def _sealer() -> KeySealer:
    return KeySealer(kek_source=lambda: make_keyring(f"primary:{_KEK}"))


def test_seal_open_roundtrip():
    ct, dw = _sealer().seal(_KEY, provider_id=_PID_A)
    assert json.loads(ct)["alg"] == "A256GCM" and json.loads(dw)["alg"] == "A256GCM"
    assert _sealer().open(ct, dw, provider_id=_PID_A) == _KEY


def test_seal_per_row_fresh_dek():
    """同一明文两次 seal（不同行）→ 密文不同（每行全新 DEK）。"""
    a = _sealer().seal(_KEY, provider_id=_PID_A)
    b = _sealer().seal(_KEY, provider_id=_PID_B)
    assert a[0] != b[0] and a[1] != b[1]


def test_open_wrong_aad_fails():
    """AAD 不匹配（跨行搬用）→ EncryptionError。"""
    ct, dw = _sealer().seal(_KEY, provider_id=_PID_A)
    with pytest.raises(EncryptionError):
        _sealer().open(ct, dw, provider_id=_PID_B)


def test_open_tampered_ciphertext_fails():
    ct, dw = _sealer().seal(_KEY, provider_id=_PID_A)
    bad = json.loads(ct)
    bad["ciphertext"] = bad["ciphertext"][:-4] + "AAAA"
    with pytest.raises(EncryptionError):
        _sealer().open(json.dumps(bad), dw, provider_id=_PID_A)


def test_open_malformed_json_ciphertext_fails():
    """Text 列内容损坏（非 JSON）→ 统一 EncryptionError（不逃逸 JSONDecodeError）。"""
    _, dw = _sealer().seal(_KEY, provider_id=_PID_A)
    with pytest.raises(EncryptionError, match="信封不是合法 JSON"):
        _sealer().open("{not-json", dw, provider_id=_PID_A)


def test_open_malformed_json_dek_wrapped_fails():
    """对称路径：dek_wrapped 非 JSON → 统一 EncryptionError。"""
    ct, _ = _sealer().seal(_KEY, provider_id=_PID_A)
    with pytest.raises(EncryptionError, match="信封不是合法 JSON"):
        _sealer().open(ct, "{not-json", provider_id=_PID_A)


def test_key_sealer_requires_configured_kek(monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY_ENCRYPTION_KEY", "")
    with pytest.raises(ValueError, match="PROVIDER_KEY_ENCRYPTION_KEY"):
        provider_crypto.key_sealer().seal(_KEY, provider_id=_PID_A)


def test_no_key_material_in_logs(caplog):
    """红线：seal/open 全路径零日志；失败路径异常链不携 Key。"""
    with caplog.at_level(logging.DEBUG):
        ct, dw = _sealer().seal(_KEY, provider_id=_PID_A)
        _sealer().open(ct, dw, provider_id=_PID_A)
        with pytest.raises(EncryptionError):
            _sealer().open(ct, dw, provider_id=_PID_B)
    assert _KEY not in caplog.text
    assert "sk-test" not in caplog.text


def test_aad_format():
    assert provider_key_aad(_PID_A) == f"agentcraft:user_providers:{_PID_A}:key:v1"
    assert provider_dek_aad(_PID_A) == f"agentcraft:user_providers:{_PID_A}:dek:v1"
