"""Provider Proxy：薄代理，按任务令牌路由（手册 §7.7，阶段 6 核心提前落地）。

链路：Pi 容器 --Bearer JWT 任务令牌--> 本服务 --解密/兑换 Key--> 对应上游
- 认证：JWT（TASK_TOKEN_SECRET 签名）解出 task_id/model scope，无状态；
  V2 双轨（Phase 8 T5b，Sup §10.10）：V2 aud 令牌（decode_v2_task_token）先行
  识别 → 控制面 /internal/provider-grant 兑换（X-Proxy-Grant-Secret 凭据头 +
  X-Task-Token）→ Key 进程内存缓存（per round_id，无持久化）→ model scope 依
  grant 下发强制；V1 aud 快照分支原样保留（D15-Option1 存续语义，冻结面）
- 路由：V1 task.provider_snapshot → source=user 解密信封 Key 转发
  snapshot.base_url；source=system 转发 PROVIDER_PROXY_UPSTREAM + OPENAI_API_KEY。
  V2 → grant 下发 base_target 直连
- 协议：/v1/chat/completions 流式透传（Pi 经扩展注册 openai-completions，
  不触达 Responses API）；/v1/models 返回任务快照模型（V1 分支）
- 纪律：真实 Key 只在本进程内存出现；日志/错误不落 Key；无 CONNECT、
  不做通用转发；model scope 与令牌/grant 一致才放行；grant 不可达/被拒
  一律 502 统一错误面（不泄控制面细节）

运行：uvicorn backend.provider_proxy:app --host 0.0.0.0 --port 8080
（容器形态见 manager.ensure_proxy；测试经 app.dependency_overrides 替换
get_proxy_session 与 app.state.upstream_client / grant_client）
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.services.task_token import TaskTokenInvalid, decode_task_token
from backend.utils.crypto import EncryptionError, decrypt_text, make_keyring, provider_key_aad
from backend.v2.task_token import TaskTokenInvalid as V2TaskTokenInvalid
from backend.v2.task_token import decode_v2_task_token

logger = logging.getLogger("agentcraft.proxy")

app = FastAPI(title="AgentCraft Provider Proxy", docs_url=None, redoc_url=None)

# 上游客户端（测试注入 httpx.MockTransport）
app.state.upstream_client: httpx.AsyncClient | None = None

# 控制面 grant 兑换客户端（测试注入 httpx.MockTransport）
app.state.grant_client: httpx.AsyncClient | None = None

# grant 兑换凭据头（Sup §10.10 钉一：proxy 专用凭据，env PROXY_GRANT_SECRET）
_GRANT_SECRET_HEADER = "X-Proxy-Grant-Secret"
_GRANT_TIMEOUT = httpx.Timeout(30.0)


async def get_proxy_session() -> AsyncGenerator[AsyncSession, None]:
    from backend.database import async_session_factory

    async with async_session_factory() as session:
        yield session


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message}}, status_code=status)


def _auth_claims(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise TaskTokenInvalid("missing bearer")
    return decode_task_token(auth[len("Bearer ") :].strip())


def _upstream_client() -> httpx.AsyncClient:
    if app.state.upstream_client is None:
        app.state.upstream_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
    return app.state.upstream_client


async def _load_task_snapshot(session: AsyncSession, task_id: int) -> dict | None:
    """查任务 → provider_snapshot（附带 _user_id 供 Key AAD 绑定校验）。"""
    from sqlalchemy import select

    from backend.models.task import Task

    row = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if row is None:
        return None
    snapshot = json.loads(row.provider_snapshot or "{}")
    if not snapshot:
        return None
    return {**snapshot, "_user_id": row.user_id}


async def _preflight(session: AsyncSession, claims: dict, body: dict):
    """model scope 与任务存在性前置检查；返回错误响应或 None。"""
    if body.get("model") != claims["model"]:
        return _error(403, "model not allowed for this task token")
    snapshot = await _load_task_snapshot(session, claims["task_id"])
    if snapshot is None:
        return _error(404, "task not found")
    return None


def _proxy_keyring(settings: Settings) -> dict[str, bytes]:
    _, keyring = make_keyring(
        settings.MCP_ENCRYPTION_KEYRING, active_kid=settings.MCP_ENCRYPTION_ACTIVE_KID
    )
    return keyring


def _resolve_upstream(snapshot: dict, settings: Settings) -> tuple[str, str | None]:
    """返回 (upstream_base, api_key|None)。key=None 表示免钥上游（如 Ollama）。"""
    if snapshot.get("source") == "user":
        encrypted = snapshot.get("api_key_encrypted")
        key = None
        if encrypted:
            try:
                key = decrypt_text(
                    encrypted,
                    aad=provider_key_aad(snapshot["_user_id"]),
                    keyring=_proxy_keyring(settings),
                )
            except EncryptionError as exc:
                raise RuntimeError("provider key decrypt failed") from exc
        return (snapshot.get("base_url") or "").rstrip("/"), key
    # 系统默认：Key 在 proxy 侧环境
    return settings.PROVIDER_PROXY_UPSTREAM.rstrip("/"), settings.OPENAI_API_KEY or None


async def _forward_upstream(
    upstream_base: str, api_key: str | None, body: dict, label: str
) -> tuple[httpx.Response | None, Response | None]:
    """按既定路由转发 /chat/completions；返回 (upstream_response, None) 或
    (None, error_response)。V1 快照路由与 V2 grant 路由共用的转发机构。"""
    if not upstream_base:
        return None, _error(502, "provider base_url missing")

    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    url = f"{upstream_base}/chat/completions"
    client = _upstream_client()
    try:
        upstream_request = client.build_request("POST", url, json=body, headers=headers)
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        logger.warning("Task %s 上游连接失败: %s", label, type(exc).__name__)
        return None, _error(502, "upstream connection failed")
    return upstream_response, None


async def _send_upstream(
    snapshot: dict, settings: Settings, body: dict, claims: dict
) -> tuple[httpx.Response | None, Response | None]:
    """V1 路由解析 + 转发；返回 (upstream_response, None) 或 (None, error_response)。"""
    try:
        upstream_base, api_key = _resolve_upstream(snapshot, settings)
    except RuntimeError as exc:
        logger.error("Task %s proxy 路由失败: %s", claims["task_id"], exc)
        return None, _error(502, "provider routing failed")
    return await _forward_upstream(upstream_base, api_key, body, str(claims["task_id"]))


async def _shape_upstream_response(upstream_response: httpx.Response) -> Response:
    """上游响应整形（V1/V2 共用）：错误原样透传 / SSE 原样中继 / JSON 缓冲透传。"""
    passthrough_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() in ("content-type", "cache-control")
    }
    if upstream_response.status_code >= 400:
        raw = await upstream_response.aread()
        await upstream_response.aclose()
        # 上游错误原样透传（属上游响应，不含本地 Key）
        return Response(
            raw,
            status_code=upstream_response.status_code,
            media_type=passthrough_headers.get("content-type", "application/json"),
        )

    content_type = passthrough_headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # 流式补全（stream=true）：SSE 原样中继
        async def relay() -> AsyncGenerator[bytes, None]:
            try:
                try:
                    async for chunk in upstream_response.aiter_raw():
                        yield chunk
                except httpx.StreamConsumed:
                    # 内容已物化的响应（测试 Mock / 非增量上游）：回退缓存读取
                    yield await upstream_response.aread()
            finally:
                await upstream_response.aclose()

        return StreamingResponse(
            relay(), status_code=upstream_response.status_code, headers=passthrough_headers
        )

    # 非流式 JSON：缓冲透传
    raw = await upstream_response.aread()
    await upstream_response.aclose()
    return Response(
        raw,
        status_code=upstream_response.status_code,
        media_type=content_type or "application/json",
    )


def _grant_cache() -> dict[str, dict]:
    """grant 结果缓存（Sup §10.10 缓存钉）：真实 Key 仅本进程内存，按 round_id
    键控、无持久化——轮终局容器死亡即持有者消失；进程重启缓存自然清零。"""
    cache = getattr(app.state, "grant_cache", None)
    if cache is None:
        cache = {}
        app.state.grant_cache = cache
    return cache


def _grant_http_client() -> httpx.AsyncClient:
    if app.state.grant_client is None:
        app.state.grant_client = httpx.AsyncClient(timeout=_GRANT_TIMEOUT)
    return app.state.grant_client


async def _fetch_grant(token: str, settings: Settings) -> dict | None:
    """控制面 /internal/provider-grant 兑换（凭据头 + X-Task-Token）。

    任何失败（不可达/非 200/响应形态非法）→ None，调用方统一 502（不泄
    控制面细节）；日志只记类型与状态码，不记凭据/Key/令牌值。"""
    headers = {_GRANT_SECRET_HEADER: settings.PROXY_GRANT_SECRET, "X-Task-Token": token}
    try:
        response = await _grant_http_client().post(
            settings.PROVIDER_PROXY_GRANT_URL, headers=headers
        )
    except httpx.HTTPError as exc:
        logger.warning("provider-grant 兑换失败: %s", type(exc).__name__)
        return None
    if response.status_code != 200:
        logger.warning("provider-grant 兑换被拒: status=%s", response.status_code)
        return None
    try:
        payload = response.json()
        return {
            "model": payload["model"],
            "api_key": payload["provider"]["api_key"],
            "base_target": payload["provider"]["base_target"],
        }
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("provider-grant 响应形态非法: %s", type(exc).__name__)
        return None


async def _chat_completions_v2(
    token: str, claims: dict, body: dict, settings: Settings
) -> Response:
    """V2 分支（Sup §10.10）：grant 兑换（进程内存缓存 per round_id）→
    model scope 依 grant 下发强制（钉四）→ base_target 直连上游。"""
    cache = _grant_cache()
    grant = cache.get(claims["round_id"])
    if grant is None:
        grant = await _fetch_grant(token, settings)
        if grant is None:
            return _error(502, "provider routing failed")
        cache[claims["round_id"]] = grant
    if body.get("model") != grant["model"]:
        return _error(403, "model not allowed for this task token")
    upstream_response, error_response = await _forward_upstream(
        str(grant["base_target"]).rstrip("/"), grant["api_key"], body, claims["round_id"]
    )
    if error_response is not None:
        return error_response
    return await _shape_upstream_response(upstream_response)


@app.get("/v1/models")
async def list_models(
    request: Request, session: AsyncSession = Depends(get_proxy_session)
) -> Response:
    try:
        claims = _auth_claims(request)
    except TaskTokenInvalid:
        return _error(401, "invalid task token")
    snapshot = await _load_task_snapshot(session, claims["task_id"])
    if snapshot is None:
        return _error(404, "task not found")
    return JSONResponse(
        {
            "object": "list",
            "data": [{"id": snapshot.get("model_id"), "object": "model", "owned_by": "agentcraft"}],
        }
    )


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    session: AsyncSession = Depends(get_proxy_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """认证 → model scope → 路由上游 → 透传。

    V2 令牌（D16①）先行识别：V2 aud 解码成功即走 grant 兑换分支（Key 进程
    内存缓存）；解码失败静默回退 V1 快照分支（D15-Option1 存续语义，冻结面不删）。
    """
    auth = request.headers.get("authorization", "")
    token = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
    try:
        v2_claims = decode_v2_task_token(token)
    except V2TaskTokenInvalid:
        v2_claims = None

    if v2_claims is not None:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _error(400, "invalid json body")
        return await _chat_completions_v2(token, v2_claims, body, settings)

    # V1 分支（§7.7 快照路径；D15-Option1 存续，冻结面不删）
    try:
        claims = _auth_claims(request)
    except TaskTokenInvalid:
        return _error(401, "invalid task token")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(400, "invalid json body")

    preflight_error = await _preflight(session, claims, body)
    if preflight_error is not None:
        return preflight_error
    snapshot = await _load_task_snapshot(session, claims["task_id"])

    upstream_response, error_response = await _send_upstream(snapshot, settings, body, claims)
    if error_response is not None:
        return error_response
    return await _shape_upstream_response(upstream_response)
