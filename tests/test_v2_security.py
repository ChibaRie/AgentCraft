from backend.v2 import security


def test_password_roundtrip_and_rejection():
    h = security.hash_password("correct horse")
    assert h != "correct horse" and h.startswith("$argon2id$")
    assert security.verify_password("correct horse", h) is True
    assert security.verify_password("wrong", h) is False


def test_generate_token_unique_and_hash_stable():
    a, b = security.generate_token(), security.generate_token()
    assert a != b and len(a) >= 32
    assert security.hash_token(a) == security.hash_token(a)
    assert len(security.hash_token(a)) == 64 and security.hash_token(a) != a


def test_device_label_sanitized():
    assert security.device_label_from_ua("Mozilla/5.0 \x00\x1b bad") == "Mozilla/5.0 bad"
    assert security.device_label_from_ua(None) == "未知设备"
    assert len(security.device_label_from_ua("x" * 500)) == 200
