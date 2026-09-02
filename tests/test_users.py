"""User system API tests.

Covers Engineering Spec v0.4.0 §6.2 (register / login / me / apply-expert)
and PRD v0.4.1 §4.1 business rules:

- 响应信封：成功 {data: ...}，失败 {error: {code, message}}（手册 §6.1）
- username/email 去首尾空格后校验；username 2-30 字符；password >= 6 位
- username/email 唯一冲突返回 409；凭证错误返回 401
- 申请专家身份申请即通过，重复申请返回 409
"""

import pytest

pytestmark = pytest.mark.usefixtures("client")


def register(client, username="alice", email="alice@example.com", password="secret123"):
    return client.post(
        "/api/auth/register",
        json={"username": username, "email": email, "password": password},
    )


def login(client, login_value="alice", password="secret123"):
    return client.post("/api/auth/login", json={"login": login_value, "password": password})


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# POST /api/auth/register
# ---------------------------------------------------------------------------


def test_register_success_returns_token_and_user(client):
    response = register(client)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["id"] > 0
    assert data["username"] == "alice"
    assert data["email"] == "alice@example.com"
    assert data["role"] == "user"
    assert isinstance(data["token"], str) and len(data["token"]) > 20


def test_register_strips_whitespace_before_validation(client):
    response = register(client, username="  bob  ", email=" bob@example.com ")
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["username"] == "bob"
    assert data["email"] == "bob@example.com"


def test_register_rejects_username_shorter_than_2_chars(client):
    # PRD §4.1.3：去首尾空格后计长，" a " 剥离后仅 1 字符
    response = register(client, username=" a ")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_register_rejects_username_longer_than_30_chars(client):
    response = register(client, username="x" * 31)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_register_rejects_invalid_email(client):
    response = register(client, email="not-an-email")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_register_rejects_short_password(client):
    response = register(client, password="abc12")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_register_rejects_password_over_72_bytes(client):
    # bcrypt 只处理前 72 字节，超长部分静默失效；在边界处直接拒绝
    response = register(client, password="a" * 73)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_register_accepts_72_byte_password(client):
    response = register(client, username="edge72", password="a" * 72)
    assert response.status_code == 201


def test_register_rejects_null_byte_password(client):
    # bcrypt 明确拒绝 NUL 字节；应在校验层返回 400 而非 500
    response = register(client, password="secret\x00x")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_login_with_null_byte_password_returns_401(client):
    # 登录侧 NUL 字节按凭证错误处理（哑哈希路径同样抛 ValueError）
    register(client)
    response = login(client, password="secret\x00x")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_register_rejects_duplicate_username(client):
    first = register(client)
    assert first.status_code == 201
    duplicate = register(client, email="other@example.com")
    assert duplicate.status_code == 409
    error = duplicate.json()["error"]
    assert error["code"] == "USERNAME_EXISTS"
    assert "alice" in error["message"]


def test_register_rejects_duplicate_email(client):
    first = register(client)
    assert first.status_code == 201
    duplicate = register(client, username="bob")
    assert duplicate.status_code == 409
    error = duplicate.json()["error"]
    assert error["code"] == "EMAIL_EXISTS"
    assert "alice@example.com" in error["message"]


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------


def test_login_success_with_username(client):
    register(client)
    response = login(client, login_value="alice")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["username"] == "alice"
    assert data["role"] == "user"
    assert isinstance(data["token"], str) and len(data["token"]) > 20


def test_login_success_with_email(client):
    register(client)
    response = login(client, login_value="alice@example.com")
    assert response.status_code == 200
    assert response.json()["data"]["id"] > 0


def test_login_with_surrounding_whitespace(client):
    register(client)
    response = login(client, login_value="  alice  ")
    assert response.status_code == 200


def test_login_wrong_password_returns_401(client):
    register(client)
    response = login(client, password="wrong-pass")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "INVALID_CREDENTIALS"
    assert error["message"] == "账号或密码不正确"


def test_login_unknown_account_returns_401(client):
    response = login(client, login_value="nobody", password="whatever")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "INVALID_CREDENTIALS"
    # 与密码错误的提示一致，避免账号枚举
    assert error["message"] == "账号或密码不正确"


# ---------------------------------------------------------------------------
# GET /api/users/me
# ---------------------------------------------------------------------------


def test_me_requires_authentication(client):
    response = client.get("/api/users/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_me_rejects_invalid_token(client):
    response = client.get("/api/users/me", headers=auth_header("not-a-jwt"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_me_returns_profile(client):
    token = register(client).json()["data"]["token"]
    response = client.get("/api/users/me", headers=auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["username"] == "alice"
    assert data["email"] == "alice@example.com"
    assert data["role"] == "user"
    assert data["created_at"]


def test_me_rejects_garbage_after_bearer(client):
    token = register(client).json()["data"]["token"]
    response = client.get("/api/users/me", headers=auth_header(f"{token}-tampered"))
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/users/me/expert
# ---------------------------------------------------------------------------


def test_apply_expert_updates_role(client):
    token = register(client).json()["data"]["token"]
    response = client.post("/api/users/me/expert", headers=auth_header(token))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["role"] == "expert"
    assert data["username"] == "alice"

    profile = client.get("/api/users/me", headers=auth_header(token)).json()["data"]
    assert profile["role"] == "expert"


def test_apply_expert_twice_returns_409(client):
    token = register(client).json()["data"]["token"]
    first = client.post("/api/users/me/expert", headers=auth_header(token))
    assert first.status_code == 200
    second = client.post("/api/users/me/expert", headers=auth_header(token))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ALREADY_EXPERT"


def test_apply_expert_requires_authentication(client):
    response = client.post("/api/users/me/expert")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# 验收主流程：注册 -> 登录 -> 个人信息 -> 申请专家身份
# ---------------------------------------------------------------------------


def test_acceptance_flow_register_login_me_expert(client):
    registered = register(client).json()["data"]
    logged_in = login(client, login_value="alice@example.com").json()["data"]
    assert logged_in["id"] == registered["id"]

    profile = client.get("/api/users/me", headers=auth_header(logged_in["token"]))
    assert profile.status_code == 200
    assert profile.json()["data"]["role"] == "user"

    applied = client.post("/api/users/me/expert", headers=auth_header(logged_in["token"]))
    assert applied.status_code == 200
    assert applied.json()["data"]["role"] == "expert"

    refreshed = client.get("/api/users/me", headers=auth_header(logged_in["token"]))
    assert refreshed.json()["data"]["role"] == "expert"


# ---------------------------------------------------------------------------
# 权限依赖：require_expert_role（专家用户判定，供后续专家管理接口使用）
# ---------------------------------------------------------------------------


def test_require_expert_role_allows_expert():
    from backend.middleware.permission import require_expert_role
    from backend.models.user import User

    expert = User(username="expert", email="expert@example.com", password_hash="x", role="expert")
    assert require_expert_role(expert) is expert


def test_require_expert_role_rejects_normal_user():
    from backend.middleware.permission import require_expert_role
    from backend.models.user import User

    normal = User(username="user", email="user@example.com", password_hash="x", role="user")

    with pytest.raises(Exception) as exc_info:
        require_expert_role(normal)
    assert getattr(exc_info.value, "status_code", None) == 403
