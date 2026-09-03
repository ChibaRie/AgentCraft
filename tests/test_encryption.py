"""AES-256-GCM 信封加密测试（手册 §11.3 契约，Provider Key 与 MCP env 同款方案换 AAD）。

- 信封 JSON：{v:1, alg:A256GCM, kid, nonce, ciphertext, tag}；nonce 12B、tag 16B，
  base64url 无填充，tag 独立存放
- AAD 域分离：换 AAD 解密必须失败；密文篡改必须失败
- keyring 路由：以信封 kid 取钥解密；加密恒用 active kid
- 规则：密钥不与 JWT SECRET_KEY 复用；明文/密钥不进日志（本模块不打日志）
"""

import base64
import os

import pytest

from backend.utils.crypto import (
    EncryptionError,
    decrypt_text,
    encrypt_text,
    make_keyring,
    mask_key_hint,
    provider_key_aad,
)

KID = "primary"
KEY = os.urandom(32)


@pytest.fixture()
def keyring() -> dict:
    return {KID: KEY}


def b64url_len(value: str) -> int:
    pad = "=" * (-len(value) % 4)
    return len(base64.urlsafe_b64decode(value + pad))


def test_roundtrip(keyring):
    envelope = encrypt_text("sk-secret-123", aad="aad-x", keyring=keyring, active_kid=KID)
    assert decrypt_text(envelope, aad="aad-x", keyring=keyring) == "sk-secret-123"


def test_envelope_shape(keyring):
    envelope = encrypt_text("sk-secret", aad="aad-x", keyring=keyring, active_kid=KID)
    assert envelope["v"] == 1
    assert envelope["alg"] == "A256GCM"
    assert envelope["kid"] == KID
    assert b64url_len(envelope["nonce"]) == 12
    assert b64url_len(envelope["tag"]) == 16
    assert "=" not in envelope["nonce"] + envelope["ciphertext"] + envelope["tag"]
    # ciphertext 不含 tag（独立存放）
    plaintext_len = len("sk-secret".encode())
    assert b64url_len(envelope["ciphertext"]) == plaintext_len


def test_wrong_aad_rejected(keyring):
    envelope = encrypt_text("sk-secret", aad="aad-right", keyring=keyring, active_kid=KID)
    with pytest.raises(EncryptionError):
        decrypt_text(envelope, aad="aad-wrong", keyring=keyring)


def test_tampered_ciphertext_rejected(keyring):
    envelope = encrypt_text("sk-secret", aad="aad-x", keyring=keyring, active_kid=KID)
    corrupted = bytearray(b64url_len(envelope["ciphertext"]))
    import base64 as b64

    envelope = {**envelope, "ciphertext": b64.urlsafe_b64encode(bytes(corrupted)).decode().rstrip("=")}
    with pytest.raises(EncryptionError):
        decrypt_text(envelope, aad="aad-x", keyring=keyring)


def test_keyring_routing_and_rotation(keyring):
    old_key = os.urandom(32)
    ring = {KID: KEY, "old": old_key}
    envelope = encrypt_text("sk-1", aad="a", keyring={"old": old_key}, active_kid="old")
    assert envelope["kid"] == "old"
    # 旧信封可在轮换后的 keyring 中解密（按 kid 取钥）
    assert decrypt_text(envelope, aad="a", keyring=ring) == "sk-1"
    # 未知 kid 拒绝
    with pytest.raises(EncryptionError):
        decrypt_text(envelope, aad="a", keyring={KID: KEY})


def test_unicode_roundtrip(keyring):
    envelope = encrypt_text("密钥🔑测试", aad="a", keyring=keyring, active_kid=KID)
    assert decrypt_text(envelope, aad="a", keyring=keyring) == "密钥🔑测试"


def test_provider_key_aad_binds_user():
    assert provider_key_aad(7) == "agentcraft:user_providers:7:api_key:v1"


def test_make_keyring_parses_spec_format():
    raw = base64.urlsafe_b64encode(KEY).decode().rstrip("=")
    old = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    kid, ring = make_keyring(f"primary:{raw},old:{old}")
    assert kid == "primary"
    assert ring["primary"] == KEY and len(ring["old"]) == 32


def test_make_keyring_rejects_bad_entries():
    with pytest.raises(EncryptionError):
        make_keyring("primary:tooshort")  # 非 32 字节
    with pytest.raises(EncryptionError):
        make_keyring("no-colon-entry")
    with pytest.raises(EncryptionError):
        make_keyring("")


def test_mask_key_hint():
    assert mask_key_hint("sk-abcdef123456") == "****3456"
    assert mask_key_hint("abc") == "****"
    assert mask_key_hint("") == ""


def test_empty_plaintext_rejected(keyring):
    with pytest.raises(EncryptionError):
        encrypt_text("", aad="a", keyring=keyring, active_kid=KID)
