"""Provider Proxy 测试（§7.7 薄代理：按任务令牌路由）。

- 认证：无/坏令牌 401
- model scope：请求体 model 与令牌不一致 403
- 路由：user 来源解密信封 Key + 转发 snapshot.base_url；system 来源走
  .env 上游与系统 Key；免钥来源不发 Authorization
- 透传：上游状态码/流式体原样返回；上游不可达 502
- Key 纪律：解密仅在路由内；上游收到的 Authorization 是真实 Key 而非任务令牌
"""

import base64
import json
import os

import httpx
import pytest
from httpx import ASGITransport, MockTransport
from httpx import Response as XResponse

from backend.config import Settings, get_settings
from backend.models.task import Task
from backend.provider_proxy import app, get_proxy_session
from backend.utils.crypto import encrypt_text, provider_key_aad

pytestmark = pytest.mark.usefixtures("client")

KEY = os.urandom(32)
KID = "primary"


def make_settings(**overrides):
    raw = base64.urlsafe_b64encode(KEY).decode().rstrip("=")
    return Settings(
        MCP_ENCRYPTION_ACTIVE_KID=KID,
        MCP_ENCRYPTION_KEYRING=f"{KID}:{raw}",
        SECRET_KEY="proxy-test-secret",
        **overrides,
    )


@pytest.fixture()
def proxy_env(test_db):
    """代理应用 + 测试设置 + 假上游收集器。"""
    settings = make_settings()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_session] = lambda: test_db.session_factory()
    captured = {}

    def upstream_handler(request: httpx.Request) -> XResponse:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return XResponse(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            },
            headers={"content-type": "application/json"},
        )

    app.state.upstream_client = httpx.AsyncClient(transport=MockTransport(upstream_handler))
    yield settings, captured
    app.dependency_overrides.pop(get_settings, None)
    app.dependency_overrides.pop(get_proxy_session, None)
    app.state.upstream_client = None


async def seed_task(test_db, user_id, snapshot):
    from backend.models.expert import Expert
    from backend.models.user import User

    async with test_db.session_factory() as session:
        session.add(
            User(
                username=f"proxy-u{user_id}",
                email=f"proxy-u{user_id}@example.com",
                password_hash="x",
                role="user",
            )
        )
        await session.flush()
        session.add(
            Expert(
                owner_id=user_id,
                name="种子专家",
                description="代理测试用专家",
                category="tech",
                persona="p",
                methodology="m",
            )
        )
        await session.flush()
        task = Task(
            user_id=user_id,
            expert_id=1,
            expert_name_snapshot="专家",
            title="t",
            status="running",
            skill_snapshot="{}",
            mcp_snapshot="{}",
            provider_snapshot=json.dumps(snapshot),
            workdir="/workspaces/authorized",
        )
        session.add(task)
        await session.commit()
        return task.id


def make_snapshot(source, base_url, model="deepseek-chat", key=None, user_id=1):
    envelope = (
        encrypt_text(
            key, aad=provider_key_aad(user_id), keyring={KID: KEY}, active_kid=KID
        )
        if key
        else None
    )
    return {
        "source": source,
        "protocol": "openai",
        "base_url": base_url,
        "model_id": model,
        "api_key_encrypted": envelope,
        "loaded_at": "2026-09-03T00:00:00Z",
    }


async def call_proxy(token, model="deepseek-chat"):
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        return await client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {token}"},
        )


def auth_headers_for(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 认证与 scope
# ---------------------------------------------------------------------------


async def test_missing_token_401():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        response = await client.post("/v1/chat/completions", json={"model": "m"})
    assert response.status_code == 401


async def test_bad_token_401():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "m"},
            headers=auth_headers_for("garbage"),
        )
    assert response.status_code == 401


async def test_model_scope_enforced(proxy_env, test_db):
    from backend.services.task_token import create_task_token

    task_id = await seed_task(test_db, 1, make_snapshot("user", "https://api.deepseek.com/v1"))
    token = create_task_token(task_id, "inst-1", "deepseek-chat")
    response = await call_proxy(token, model="other-model")
    assert response.status_code == 403


async def test_missing_task_404(proxy_env, test_db):
    from backend.services.task_token import create_task_token

    token = create_task_token(99999, "inst-1", "deepseek-chat")
    response = await call_proxy(token)
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 路由与 Key 处理
# ---------------------------------------------------------------------------


async def test_user_provider_routes_with_decrypted_key(proxy_env, test_db):
    settings, captured = proxy_env
    from backend.services.task_token import create_task_token

    task_id = await seed_task(
        test_db, 1, make_snapshot("user", "https://api.deepseek.com/v1", key="sk-real-9999")
    )
    token = create_task_token(task_id, "inst-1", "deepseek-chat")
    response = await call_proxy(token)
    assert response.status_code == 200
    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    # 上游收到的是真实 Key，不是任务令牌
    assert captured["auth"] == "Bearer sk-real-9999"
    # 响应不含任何 Key 形态
    assert "sk-real" not in response.text


async def test_system_provider_routes_to_env_upstream(proxy_env, test_db):
    settings, captured = proxy_env
    from backend.services.task_token import create_task_token

    task_id = await seed_task(test_db, 1, make_snapshot("system", "http://provider-proxy:8080/v1"))
    token = create_task_token(task_id, "inst-1", "gpt-4o-mini")
    # 系统模式的 model scope 用快照 model
    response = await call_proxy(token, model="gpt-4o-mini")
    assert response.status_code == 200
    assert captured["url"] == "https://api.openai.com/chat/completions"


async def test_keyless_provider_sends_no_auth(proxy_env, test_db):
    settings, captured = proxy_env
    from backend.services.task_token import create_task_token

    task_id = await seed_task(
        test_db, 1, make_snapshot("user", "http://host.docker.internal:11434/v1", key=None)
    )
    token = create_task_token(task_id, "inst-1", "deepseek-chat")
    response = await call_proxy(token)
    assert response.status_code == 200
    assert "authorization" not in captured  # 免钥上游不带认证头


async def test_upstream_error_passthrough(proxy_env, test_db):
    settings, captured = proxy_env
    from backend.services.task_token import create_task_token

    def failing_handler(request: httpx.Request) -> XResponse:
        return XResponse(
            401,
            json={"error": {"message": "bad key"}},
            headers={"content-type": "application/json"},
        )

    app.state.upstream_client = httpx.AsyncClient(transport=MockTransport(failing_handler))
    task_id = await seed_task(
        test_db, 1, make_snapshot("user", "https://api.deepseek.com/v1", key="sk-wrong")
    )
    token = create_task_token(task_id, "inst-1", "deepseek-chat")
    response = await call_proxy(token)
    assert response.status_code == 401
    assert "bad key" in response.text


async def test_upstream_unreachable_502(proxy_env, test_db):
    from backend.services.task_token import create_task_token

    def unreachable(request: httpx.Request) -> XResponse:
        raise httpx.ConnectError("refused")

    app.state.upstream_client = httpx.AsyncClient(transport=MockTransport(unreachable))
    task_id = await seed_task(
        test_db, 1, make_snapshot("user", "https://unreachable.invalid/v1", key="sk-x")
    )
    token = create_task_token(task_id, "inst-1", "deepseek-chat")
    response = await call_proxy(token)
    assert response.status_code == 502


async def test_models_endpoint_returns_snapshot_model(proxy_env, test_db):
    from backend.services.task_token import create_task_token

    task_id = await seed_task(test_db, 1, make_snapshot("user", "https://api.deepseek.com/v1"))
    token = create_task_token(task_id, "inst-1", "deepseek-chat")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        response = await client.get("/v1/models", headers=auth_headers_for(token))
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "deepseek-chat"


async def test_sse_streaming_passthrough(proxy_env, test_db):
    """stream=true 的 SSE 上游：原样中继事件流。"""
    from backend.services.task_token import create_task_token

    def sse_handler(request: httpx.Request) -> XResponse:
        return XResponse(
            200,
            content=b'data: {"choices":[{"delta":{"content":"\\""}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    app.state.upstream_client = httpx.AsyncClient(transport=MockTransport(sse_handler))
    task_id = await seed_task(
        test_db, 1, make_snapshot("user", "https://api.deepseek.com/v1", key="sk-sse")
    )
    token = create_task_token(task_id, "inst-1", "deepseek-chat")
    response = await call_proxy(token)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert "[DONE]" in response.text
