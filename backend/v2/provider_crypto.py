"""BYOK Provider Key 信封加密（DEK/KEK 两级，PlanD-T2）。

设计出处：app-layer design §4.2（KeySealer 抽象、KEK=环境变量、部署期换 VM KMS）、
Eng Spec §3（每记录 DEK AES-256-GCM 由 KEK 包装）。结构（裁决 D8/D9）：

- dek_wrapped    = encrypt_text(b64url(DEK), keyring={primary: KEK}, aad=…:dek:v1)
- key_ciphertext = encrypt_text(明文Key, keyring={primary: DEK}, aad=…:key:v1)
- key_version 是 user_providers 行级计数器（服务层维护），不在本模块；
- KEK 来源 PROVIDER_KEY_ENCRYPTION_KEY，现读不缓存；两列 Text 存紧凑 JSON 信封。

红线：本模块零日志零 DB；明文/密文/DEK 不进任何 log 调用（连 repr 都不行）。
"""

import base64
import binascii
import json
import os
from collections.abc import Callable

from backend.config import get_settings
from backend.utils.crypto import decrypt_text, encrypt_text, make_keyring

_AAD_BASE = "agentcraft:user_providers"


def provider_key_aad(provider_id: str) -> str:
    """外层信封 AAD：agentcraft:user_providers:{row_uuid}:key:v1（裁决 D8）。"""
    return f"{_AAD_BASE}:{provider_id}:key:v1"


def provider_dek_aad(provider_id: str) -> str:
    """内层信封 AAD：agentcraft:user_providers:{row_uuid}:dek:v1。"""
    return f"{_AAD_BASE}:{provider_id}:dek:v1"


def _provider_kek_keyring() -> tuple[str, dict[str, bytes]]:
    """现读 PROVIDER_KEY_ENCRYPTION_KEY（b64url 32B）→ (active_kid, keyring)。

    校验形态对齐 rate_limit._rate_limit_hmac_key；异常只透出干净校验消息。
    """
    raw = get_settings().PROVIDER_KEY_ENCRYPTION_KEY
    if not raw:
        raise ValueError("PROVIDER_KEY_ENCRYPTION_KEY 未配置（须为 b64url 编码的 32 字节密钥）")
    try:
        material = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValueError("PROVIDER_KEY_ENCRYPTION_KEY 不是合法 base64url") from exc
    if len(material) != 32:
        raise ValueError("PROVIDER_KEY_ENCRYPTION_KEY 解码后必须为 32 字节")
    return make_keyring(f"primary:{raw}")


def _dumps(envelope: dict) -> str:
    """信封 dict → 紧凑 JSON 字符串（Text 列存储形态）。"""
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"))


class KeySealer:
    """KeySealer 抽象（app-layer design §4.2）：seal/open 双层信封。

    ``kek_source`` 可注入（单元测试 / 部署期 VM KMS 替换点），默认现读环境变量。
    """

    def __init__(
        self,
        kek_source: Callable[[], tuple[str, dict[str, bytes]]] = _provider_kek_keyring,
    ) -> None:
        self._kek_source = kek_source

    def seal(self, plaintext: str, *, provider_id: str) -> tuple[str, str]:
        """明文 Key → (key_ciphertext, dek_wrapped)，每行全新 32B DEK。"""
        dek = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        kid, kek_keyring = self._kek_source()
        dek_wrapped = encrypt_text(
            dek, aad=provider_dek_aad(provider_id), keyring=kek_keyring, active_kid=kid
        )
        dek_kid, dek_keyring = make_keyring(f"primary:{dek}")
        key_ciphertext = encrypt_text(
            plaintext, aad=provider_key_aad(provider_id), keyring=dek_keyring, active_kid=dek_kid
        )
        return _dumps(key_ciphertext), _dumps(dek_wrapped)

    def open(self, key_ciphertext: str, dek_wrapped: str, *, provider_id: str) -> str:
        """双层解封：KEK 解 DEK → DEK 解明文。任一层失败统一 EncryptionError。"""
        _, kek_keyring = self._kek_source()
        dek = decrypt_text(
            json.loads(dek_wrapped), aad=provider_dek_aad(provider_id), keyring=kek_keyring
        )
        _, dek_keyring = make_keyring(f"primary:{dek}")
        return decrypt_text(
            json.loads(key_ciphertext), aad=provider_key_aad(provider_id), keyring=dek_keyring
        )


def key_sealer() -> KeySealer:
    """默认 KeySealer（每次现读 KEK，不缓存——轮换即时生效、测试可注入 env）。"""
    return KeySealer()
