"""V2 用户 MCP 面服务域（Phase 10 M2）。

契约出处：Phase 10 设计（docs/superpowers/plans/2026-09-17-v2-phase-10-mcp-face.md，
Phase 5 裁决受控逆转）；端点形态范本 = Sup §10.11 作者面 + Provider 面。本模块纪律
（与 provider_service 同源）：

- 一切 owner 读写要求调用方已置 GUC（owner_session 事务内）；本模块不 commit；
- 统一 404：行缺失一律 HTTPException NOT_FOUND（跨用户 RLS 静默 0 行同形）；
- 命令材料红线：{command, args, env} 明文只在 seal/open 的协程内存中存活，
  禁缓存/禁日志/禁入错误消息/禁入审计 detail/禁入幂等记录；
- 注册/更新/删除/发现全部同事务审计（action=mcp.server.*，audit_logs INSERT
  授权随迁移 0011，action/detail 由服务层固定字面量写入）。
"""

import json
import uuid as _uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.v2.idempotency import begin, store, subject_user
from backend.v2.ids import uuid7
from backend.v2.models import AuditLog, UserMcpServer, UserMcpTool
from backend.v2.provider_crypto import key_sealer
from backend.v2.provider_service import Replay, validate_https_url
from backend.v2.runtime import V2Runtime, owner_session

# 本域刻意零日志：命令材料红线（明文/密文/DEK 不得进入任何 log 调用）。

ROUTE_CREATE = "/api/mcp/servers"

_NOT_FOUND_DETAIL = {"code": "NOT_FOUND", "message": "资源不存在"}

_MCP_URL_MESSAGE = "url 须为 https MCP 服务器地址，不含凭据"

# 限流 scope mcp_discover（10/h/用户）登记于 rate_limit.LIMITS；本模块不重复限流。


def _server_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)


def _validation(message: str) -> HTTPException:
    # VALIDATION_ERROR 为 V1 约定形状字面（不经 ErrorCode 注册表，provider 域同款）
    return HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": message})


def _discover_failed() -> HTTPException:
    """发现失败统一 502 面：不透传上游/网络细节（Eng §6 红线）。"""
    return HTTPException(
        status_code=502,
        detail={"code": "MCP_DISCOVER_FAILED", "message": "MCP 服务器工具发现失败"},
    )


def _validate_name(name: str) -> None:
    stripped = name.strip()
    if not stripped or name != stripped:
        raise _validation("name 须为 1..80 字符且首尾无空白的字符串")


def _validate_transport_payload(updates: dict) -> None:
    """transport 与载荷交叉一致性：stdio ⇒ command（url/env/args 不得混带）、
    http ⇒ url（command/args/env 不得混带）。"""
    kind = updates["transport_kind"]
    if kind == "stdio":
        if not updates.get("command"):
            raise _validation("stdio 传输必须提供 command")
        if updates.get("url") is not None:
            raise _validation("stdio 传输不支持 url")
    else:  # http（schema Literal 已限词表）
        if not updates.get("url"):
            raise _validation("http 传输必须提供 url")
        for field in ("command", "args", "env"):
            if updates.get(field) is not None:
                raise _validation(f"http 传输不支持 {field}")
        validate_https_url(updates["url"], _MCP_URL_MESSAGE)


def _server_out(row: UserMcpServer) -> dict:
    """ORM 行 → 出参形态：零 command/env/url/密文（has_command 布尔代替）。"""
    return {
        "id": str(row.id),
        "name": row.name,
        "transport_kind": row.transport_kind,
        "enabled": row.enabled,
        "has_command": row.command_encrypted is not None,
        "created_at": row.created_at.isoformat(),
    }


async def _get_server_row(db: AsyncSession, server_id: str) -> UserMcpServer:
    """owner 事务内取行：格式非法 → 400（防裸 ValueError 落 500 兜底）；
    缺失 → 统一 404（跨用户 RLS 0 行同形）。"""
    try:
        sid = _uuid.UUID(server_id)
    except ValueError as exc:
        raise _validation("server_id 不是合法 UUID") from exc
    row = (
        await db.execute(select(UserMcpServer).where(UserMcpServer.id == sid))
    ).scalar_one_or_none()
    if row is None:
        raise _server_not_found()
    return row


async def list_servers(db: AsyncSession) -> list[dict]:
    """owner 事务内列当前用户全部 MCP server（RLS 限定；created_at ASC）。"""
    rows = (
        (await db.execute(select(UserMcpServer).order_by(UserMcpServer.created_at.asc())))
        .scalars()
        .all()
    )
    return [_server_out(row) for row in rows]


async def create_server(
    runtime: V2Runtime,
    *,
    user_id: str,
    updates: dict,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """注册 MCP server（stdio：{command, args, env} 整体 JSON 信封加密，AAD 绑定
    server id；http：url 形态校验后明文落库——url 非机密，密文面只有命令材料）。

    门序同 Provider create：幂等 begin（route=ROUTE_CREATE）→ 形态校验 →
    owner_session 单事务（落库 → 审计 → store）。
    updates 为 McpServerCreateRequest.model_dump(exclude_unset=True)。
    """
    async with runtime.app_factory() as db:
        replay = await begin(
            db,
            subject_hash=subject_user(user_id),
            route=ROUTE_CREATE,
            key=idem_key,
            req_hash=idem_hash,
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])

    _validate_name(updates["name"])
    _validate_transport_payload(updates)

    new_id = uuid7()
    detail: dict
    async with owner_session(runtime, user_id) as db:
        command_encrypted = None
        command_dek_wrapped = None
        if updates["transport_kind"] == "stdio":
            # 明文命令材料唯一存活点：本协程内 seal 一次，不留任何副本
            plaintext = json.dumps(
                {
                    "command": updates["command"],
                    "args": updates.get("args") or [],
                    "env": updates.get("env") or {},
                },
                ensure_ascii=False,
            )
            command_encrypted, command_dek_wrapped = key_sealer().seal(
                plaintext, provider_id=str(new_id)
            )
        db.add(
            UserMcpServer(
                id=new_id,
                owner_id=_uuid.UUID(user_id),
                name=updates["name"],
                transport_kind=updates["transport_kind"],
                command_encrypted=command_encrypted,
                command_dek_wrapped=command_dek_wrapped,
                url=updates.get("url"),
                enabled=True,
            )
        )
        await db.flush()
        row = await _get_server_row(db, str(new_id))  # 回读 server_default 时间戳
        detail = _server_out(row)
        db.add(
            AuditLog(
                actor_id=_uuid.UUID(user_id),
                action="mcp.server.register",
                target_type="mcp_server",
                target_id=row.id,
                reason="user mcp register",
                detail={
                    "server_id": str(row.id),
                    "name": row.name,
                    "transport_kind": row.transport_kind,
                },
            )
        )
        await store(
            db,
            subject_hash=subject_user(user_id),
            route=ROUTE_CREATE,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json={"data": detail},
        )
    return detail


_NULL_NAME_MESSAGE = "name 不支持置空（缺席=不变，字符串=替换）"
_NULL_ENABLED_MESSAGE = "enabled 不支持置空（缺席=不变，布尔=覆盖）"


async def update_server(
    runtime: V2Runtime,
    *,
    user_id: str,
    server_id: str,
    updates: dict,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """更新（name/enabled 三态：缺席=不变、显式 null 400、给定值=覆盖）。

    transport/command/url 不可经 PUT 变更（发现缓存与任务快照一致性优先，
    变更语义 = 删除后重注册）。门序同 create：幂等 begin（route=具体路径）→
    owner 事务（404 门 → 显式 null 400 → 覆盖 → 审计 → store）。
    updates 为 McpServerUpdateRequest.model_dump(exclude_unset=True)。
    """
    route = f"{ROUTE_CREATE}/{server_id}"
    async with runtime.app_factory() as db:
        replay = await begin(
            db, subject_hash=subject_user(user_id), route=route, key=idem_key, req_hash=idem_hash
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])

    detail: dict
    async with owner_session(runtime, user_id) as db:
        row = await _get_server_row(db, server_id)  # 缺失 → 404（跨用户同形）
        for field, message in (("name", _NULL_NAME_MESSAGE), ("enabled", _NULL_ENABLED_MESSAGE)):
            if field in updates and updates[field] is None:
                raise _validation(message)
        changed: list[str] = []
        if "name" in updates and updates["name"] != row.name:
            _validate_name(updates["name"])
            row.name = updates["name"]
            changed.append("name")
        if "enabled" in updates and updates["enabled"] != row.enabled:
            row.enabled = updates["enabled"]
            changed.append("enabled")
        await db.flush()
        detail = _server_out(row)
        db.add(
            AuditLog(
                actor_id=_uuid.UUID(user_id),
                action="mcp.server.update",
                target_type="mcp_server",
                target_id=row.id,
                reason="user mcp update",
                detail={
                    "server_id": str(row.id),
                    "name": row.name,
                    "transport_kind": row.transport_kind,
                    "fields": changed,
                },
            )
        )
        await store(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json={"data": detail},
        )
    return detail


async def delete_server(
    runtime: V2Runtime,
    *,
    user_id: str,
    server_id: str,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """物理删除（沿作者面 DELETE 先例）：server 行 + 发现缓存（FK CASCADE）一并
    毁灭，同事务审计（物理毁灭必须有审计痕）。DELETE 无请求体：
    request_hash(None)。幂等 begin 先于 404 门——物理删后同 key 重放 200
    原响应（§7 重放优先）。
    """
    route = f"{ROUTE_CREATE}/{server_id}"
    async with runtime.app_factory() as db:
        replay = await begin(
            db, subject_hash=subject_user(user_id), route=route, key=idem_key, req_hash=idem_hash
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])

    body: dict
    async with owner_session(runtime, user_id) as db:
        row = await _get_server_row(db, server_id)  # 404 门（重放之后）
        server_brief = {
            "server_id": str(row.id),
            "name": row.name,
            "transport_kind": row.transport_kind,
        }
        await db.delete(row)
        await db.flush()
        body = {"data": {"deleted": True, "id": server_brief["server_id"]}}
        db.add(
            AuditLog(
                actor_id=_uuid.UUID(user_id),
                action="mcp.server.delete",
                target_type="mcp_server",
                target_id=row.id,
                reason="user mcp delete",
                detail=server_brief,
            )
        )
        await store(
            db,
            subject_hash=subject_user(user_id),
            route=route,
            key=idem_key,
            req_hash=idem_hash,
            status_code=200,
            response_json=body,
        )
    return body["data"]


# ---------------------------------------------------------------------------
# 发现链（http 形态；stdio 为 501 占位——mcp-sandbox 沙箱镜像属 Phase 10 M5）
# ---------------------------------------------------------------------------

_DISCOVER_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_MAX_DISCOVER_RESPONSE_BYTES = 2 * 1024 * 1024  # 2MB 流式硬上限（provider test 同款）
_MAX_TOOLS_PER_SERVER = 100  # 发现缓存上限（防无界外部数据入库）
_MAX_TOOL_NAME_LENGTH = 200  # 对齐 user_mcp_tools.tool_name 列宽，越界工具跳过
_MCP_PROTOCOL_VERSION = "2025-03-26"


def _parse_json_rpc_body(raw: bytes) -> dict | None:
    """响应体 → JSON-RPC 消息：JSON 直解；失败按 streamable HTTP SSE 兜底
    （取最后一条含 result/error 的 data: 行）。均失败 → None。"""
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    result: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            candidate = json.loads(line[len("data:") :].strip())
        except ValueError:
            continue
        if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
            result = candidate
    return result


def _normalize_tools(raw_tools: list) -> list[dict]:
    """MCP tool 描述符 → 缓存行形态；无名/越界/非 dict 条目跳过，tool_name 去重。"""
    tools: list[dict] = []
    seen: set[str] = set()
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        tool_name = item.get("name")
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or len(tool_name) > _MAX_TOOL_NAME_LENGTH
        ):
            continue
        if tool_name in seen:
            continue
        seen.add(tool_name)
        title = item.get("title")
        schema = item.get("inputSchema")
        tools.append(
            {
                "tool_name": tool_name,
                "name": title if isinstance(title, str) and title else tool_name,
                "description": (
                    item["description"] if isinstance(item.get("description"), str) else None
                ),
                "schema_json": schema if isinstance(schema, dict) else None,
            }
        )
    return tools


async def _mcp_http_list_tools(
    url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict]:
    """MCP streamable HTTP 握手 + tools/list：POST {url}/mcp 三步
    （initialize → notifications/initialized → tools/list），回传的
    mcp-session-id 透传。任何网络/协议失败 → 统一 502（零上游细节泄漏）；
    响应体 2MB 硬上限。"""
    endpoint = url.rstrip("/") + "/mcp"
    headers = {"accept": "application/json, text/event-stream"}
    try:
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False, timeout=_DISCOVER_TIMEOUT
        ) as client:
            init = await client.post(
                endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": _MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "agentcraft", "version": "1.0"},
                    },
                },
                headers=headers,
            )
            if init.status_code // 100 != 2:
                raise _discover_failed()
            session_id = init.headers.get("mcp-session-id")
            if session_id:
                headers["mcp-session-id"] = session_id
            await client.post(  # initialized 通知：结果不入契约，失败不打断（无状态服务端可忽略）
                endpoint,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=headers,
            )
            resp = await client.post(
                endpoint,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers=headers,
            )
            if resp.status_code // 100 != 2:
                raise _discover_failed()
            total = 0
            chunks: list[bytes] = []
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_DISCOVER_RESPONSE_BYTES:
                    raise _discover_failed()
                chunks.append(chunk)
    except httpx.HTTPError:
        raise _discover_failed() from None  # 超时/连接失败/流错误：统一失败形态
    rpc = _parse_json_rpc_body(b"".join(chunks))
    if rpc is None or "error" in rpc:
        raise _discover_failed()
    raw_tools = rpc.get("result", {}).get("tools") if isinstance(rpc.get("result"), dict) else None
    if not isinstance(raw_tools, list):
        raise _discover_failed()
    if len(raw_tools) > _MAX_TOOLS_PER_SERVER:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "MCP_DISCOVER_FAILED",
                "message": f"工具数量超过上限（{_MAX_TOOLS_PER_SERVER}）",
            },
        )
    return _normalize_tools(raw_tools)


async def discover_server(
    runtime: V2Runtime,
    *,
    user_id: str,
    server_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """发现链（限流在路由层）：404 门 → stdio 占位 501 / http：出网前 SSRF
    公网校验（net_guard，provider 连通性测试同款）→ 握手拉 tools → owner
    事务内先删后插缓存（幂等）+ 同事务审计。网络请求不进 owner 事务
    （provider test 同款——读出行后即关会话，写回前二次 404 门）。
    """
    async with owner_session(runtime, user_id) as db:
        row = await _get_server_row(db, server_id)
        if row.transport_kind == "stdio":
            raise HTTPException(
                status_code=501,
                detail={
                    "code": "NOT_IMPLEMENTED",
                    "message": "stdio 发现链将在 mcp-sandbox 沙箱镜像交付后启用",
                },
            )
        url = row.url

    from backend.utils.net_guard import EgressBlockedError, assert_public_https

    try:
        assert_public_https(url)
    except EgressBlockedError:
        raise _discover_failed() from None

    tools = await _mcp_http_list_tools(url, transport=transport)

    async with owner_session(runtime, user_id) as db:
        row = await _get_server_row(db, server_id)  # 发现期间行可能被删 → 404
        await db.execute(delete(UserMcpTool).where(UserMcpTool.server_id == row.id))
        for tool in tools:
            db.add(
                UserMcpTool(
                    id=uuid7(),
                    server_id=row.id,
                    tool_name=tool["tool_name"],
                    name=tool["name"],
                    description=tool["description"],
                    schema_json=tool["schema_json"],
                    enabled=True,
                )
            )
        db.add(
            AuditLog(
                actor_id=_uuid.UUID(user_id),
                action="mcp.server.discover",
                target_type="mcp_server",
                target_id=row.id,
                reason="user mcp discover",
                detail={
                    "server_id": str(row.id),
                    "name": row.name,
                    "transport_kind": row.transport_kind,
                    "tool_count": len(tools),
                },
            )
        )
        await db.flush()
    return {
        "server_id": str(row.id),
        "transport_kind": row.transport_kind,
        "tools": [
            {"tool_name": t["tool_name"], "name": t["name"], "description": t["description"]}
            for t in tools
        ],
    }
