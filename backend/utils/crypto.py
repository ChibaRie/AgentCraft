"""AES-256-GCM 信封加密（手册 §11.3 契约）。

信封 JSON：`{v:1, alg:"A256GCM", kid, nonce, ciphertext, tag}`
- nonce 12B / tag 16B，base64url 无填充；tag 独立存放，不拼入 ciphertext
- AAD 域分离：解密必须提供与加密一致的 AAD（绑定用途与归属）
- keyring：`{kid: 32B key}`；加密恒用 active kid，解密按信封 kid 取钥（支持轮换）
- 纪律：明文与密钥不落日志（本模块零日志）；密钥不与 JWT SECRET_KEY 复用
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_TAG_BYTES = 16
_KEY_BYTES = 32


class EncryptionError(Exception):
    """加密/解密失败（AAD 不符、密文篡改、keyring 配置错误等）。"""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + pad)
    except Exception as exc:  # noqa: BLE001 - 统一为域错误
        raise EncryptionError("信封字段不是合法的 base64url") from exc


def provider_key_aad(user_id: int) -> str:
    """用户 Provider Key 的 AAD（绑定归属用户，防跨行/跨域搬用密文）。"""
    return f"agentcraft:user_providers:{int(user_id)}:api_key:v1"


def make_keyring(raw: str, active_kid: str | None = None) -> tuple[str, dict[str, bytes]]:
    """解析 `MCP_ENCRYPTION_KEYRING=primary:<b64url32B>,old:<b64url32B>`。

    active_kid（MCP_ENCRYPTION_ACTIVE_KID）显式指定写入密钥；缺省取首个条目。
    """
    if not raw or not raw.strip():
        raise EncryptionError(
            "未配置 MCP_ENCRYPTION_KEYRING（格式：kid:<base64url 32 字节密钥>[,...]）"
        )
    keyring: dict[str, bytes] = {}
    active_kid_ref: str | None = None
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            raise EncryptionError(f"keyring 条目缺少 kid 冒号分隔: {entry[:12]}…")
        kid, encoded = entry.split(":", 1)
        kid = kid.strip()
        key = _b64url_decode(encoded)
        if len(key) != _KEY_BYTES:
            raise EncryptionError(f"keyring 密钥 {kid} 必须是 32 字节（当前 {len(key)}）")
        if not kid:
            raise EncryptionError("keyring kid 不能为空")
        keyring[kid] = key
        if active_kid_ref is None:
            active_kid_ref = kid
    if active_kid_ref is None:
        raise EncryptionError("keyring 为空")
    if active_kid is not None:
        if active_kid not in keyring:
            raise EncryptionError(f"ACTIVE_KID {active_kid} 不在 keyring 中")
        active_kid_ref = active_kid
    return active_kid_ref, keyring


def encrypt_text(plaintext: str, *, aad: str, keyring: dict[str, bytes], active_kid: str) -> dict:
    """加密为信封 JSON（dict）。恒用 active kid；nonce 随机 12B。"""
    if not plaintext:
        raise EncryptionError("明文不能为空")
    if active_kid not in keyring:
        raise EncryptionError(f"active kid {active_kid} 不在 keyring 中")
    nonce = os.urandom(_NONCE_BYTES)
    # AESGCM.encrypt 返回 ciphertext||tag（tag 固定 16B 尾部），按契约拆分独立存放
    sealed = AESGCM(keyring[active_kid]).encrypt(
        nonce, plaintext.encode("utf-8"), aad.encode("utf-8")
    )
    ciphertext, tag = sealed[:-_TAG_BYTES], sealed[-_TAG_BYTES:]
    return {
        "v": 1,
        "alg": "A256GCM",
        "kid": active_kid,
        "nonce": _b64url_encode(nonce),
        "ciphertext": _b64url_encode(ciphertext),
        "tag": _b64url_encode(tag),
    }


def decrypt_text(envelope: dict, *, aad: str, keyring: dict[str, bytes]) -> str:
    """解信封；AAD 不符/密文篡改/kid 未知/字段缺失一律 EncryptionError。"""
    try:
        kid = envelope["kid"]
        nonce = _b64url_decode(envelope["nonce"])
        ciphertext = _b64url_decode(envelope["ciphertext"])
        tag = _b64url_decode(envelope["tag"])
    except (KeyError, TypeError) as exc:
        raise EncryptionError("信封字段缺失或非法") from exc
    if envelope.get("v") != 1 or envelope.get("alg") != "A256GCM":
        raise EncryptionError("信封版本/算法不支持")
    if kid not in keyring:
        raise EncryptionError(f"信封 kid {kid} 不在 keyring 中")
    if len(nonce) != _NONCE_BYTES or len(tag) != _TAG_BYTES:
        raise EncryptionError("nonce/tag 长度非法")
    sealed = ciphertext + tag
    try:
        return AESGCM(keyring[kid]).decrypt(nonce, sealed, aad.encode("utf-8")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - 统一为域错误，不泄露细节
        raise EncryptionError("解密失败（AAD 不符或密文被篡改）") from exc


def mask_key_hint(plaintext: str) -> str:
    """Key 尾 4 位掩码提示；过短一律 ****（不泄露长度信息）。"""
    if len(plaintext) < 8:
        return "****" if plaintext else ""
    return f"****{plaintext[-4:]}"
