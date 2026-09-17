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

import asyncio
import json
import os
import uuid as _uuid
from contextlib import suppress

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
# 发现链（http：streamable HTTP 握手；stdio：subprocess 直拉 + stdio 握手，
# Phase 10 M3——mcp-sandbox 镜像形态属 M5，本函数为服务面真件、测试以
# fake 进程驱动真实协议往返）
# ---------------------------------------------------------------------------

_DISCOVER_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
_MAX_DISCOVER_RESPONSE_BYTES = 2 * 1024 * 1024  # 2MB 流式硬上限（provider test 同款）
_MAX_TOOLS_PER_SERVER = 100  # 发现缓存上限（防无界外部数据入库）
_MAX_TOOL_NAME_LENGTH = 200  # 对齐 user_mcp_tools.tool_name 列宽，越界工具跳过
_MCP_PROTOCOL_VERSION = "2025-03-26"

# stdio 子进程纪律（Phase 10 M3）：整体握手 10s 超时、stdout 2MB 输出上限、
# 流式 reader 单行上限加少量余量（越限 readline 以 ValueError 浮出 → 同一 502 面）
_STDIO_TIMEOUT_SECONDS = 10.0
_STDIO_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_STDIO_STREAM_LIMIT_MARGIN = 4096


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


def _open_stdio_material(row: UserMcpServer) -> dict:
    """server 行信封解封 → 启动材料 {command, args, env}（明文仅协程内存存活；
    AAD 绑定 server id）。解封/解析/形态任一失败 → 统一 502 面。"""
    if row.command_encrypted is None or row.command_dek_wrapped is None:
        raise _discover_failed()
    try:
        plaintext = key_sealer().open(
            row.command_encrypted, row.command_dek_wrapped, provider_id=str(row.id)
        )
        material = json.loads(plaintext)
        command = material["command"]
        args = material.get("args")
        env = material.get("env")
        if not isinstance(command, str) or not command:
            raise _discover_failed()
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise _discover_failed()
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise _discover_failed()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - 解封/解析失败统一 502（零材料泄漏）
        raise _discover_failed() from None
    return {"command": command, "args": list(args), "env": dict(env)}


async def _stdio_send(proc: asyncio.subprocess.Process, payload: dict) -> None:
    """换行分隔 JSON-RPC 单帧写入（MCP stdio 传输形态）。"""
    proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
    await proc.stdin.drain()


async def _stdio_recv(
    proc: asyncio.subprocess.Process, want_id: int, *, max_output_bytes: int
) -> dict:
    """读取指定 id 的 JSON-RPC 响应行：非 JSON 行（日志/通知）跳过、EOF/输出
    超限（累计预算）→ 统一 502 面。"""
    received = 0
    while True:
        line = await proc.stdout.readline()
        if not line:
            raise _discover_failed()  # 子进程 EOF（提前退出/崩溃）
        received += len(line)
        if received > max_output_bytes:
            raise _discover_failed()
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == want_id:
            return message


def _rpc_result_of(message: dict) -> dict:
    """JSON-RPC 响应 → result 对象：error 响应/非 dict result → 统一 502 面。"""
    if "error" in message or not isinstance(message.get("result"), dict):
        raise _discover_failed()
    return message["result"]


async def _stdio_handshake(proc: asyncio.subprocess.Process, *, max_output_bytes: int) -> list:
    """MCP stdio 握手：initialize → notifications/initialized → tools/list
    （换行分隔 JSON-RPC）。任何 EOF/协议失败/输出超限 → 统一 502 面。"""
    await _stdio_send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentcraft", "version": "1.0"},
            },
        },
    )
    _rpc_result_of(await _stdio_recv(proc, 1, max_output_bytes=max_output_bytes))
    await _stdio_send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    await _stdio_send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    listing = _rpc_result_of(await _stdio_recv(proc, 2, max_output_bytes=max_output_bytes))
    raw_tools = listing.get("tools")
    if not isinstance(raw_tools, list):
        raise _discover_failed()
    return raw_tools


_STDIO_REAP_WAIT_SECONDS = 1.0


async def _terminate_stdio_process(proc: asyncio.subprocess.Process) -> None:
    """子进程回收（关 stdin → kill → 有界 wait；幂等，已退出零操作）。

    Windows Proactor 已知形态：子进程写端阻塞在满管道（超大单行输出触发
    reader 流上限后缓冲清空、写端仍堵）时 kill，``wait()`` 的进程退出通知可能
    永不送达——等待必须有界，超时即放弃收尾（进程已被 TerminateProcess 终止，
    句柄由事件循环/GC 收场），不得让发现链卡死。"""
    if proc.returncode is not None:
        return
    with suppress(Exception):  # noqa: BLE001 - 优雅退出优先：多数 server 退出于 stdin EOF
        proc.stdin.close()
    with suppress(ProcessLookupError):
        proc.kill()
    with suppress(Exception):  # noqa: BLE001 - 收尾尽力而为（见 docstring）
        await asyncio.wait_for(proc.wait(), _STDIO_REAP_WAIT_SECONDS)


async def discover_stdio(
    row: UserMcpServer,
    *,
    timeout: float = _STDIO_TIMEOUT_SECONDS,
    max_output_bytes: int = _STDIO_MAX_OUTPUT_BYTES,
) -> list[dict]:
    """stdio 发现链服务函数（Phase 10 M3）：信封解封启动配置 → subprocess 直拉 →
    MCP stdio 握手（initialize → tools/list）→ 归一化工具描述符。

    子进程纪律：整体握手 ``timeout``（缺省 10s）超时、stdout 输出
    ``max_output_bytes``（缺省 2MB）上限（累计预算 + reader 单行流上限双闸）、
    退出前 kill 回收；任何失败统一 502 MCP_DISCOVER_FAILED（零上游/材料细节
    泄漏）。``timeout``/``max_output_bytes`` 仅供测试注入收窄验证。
    """
    material = _open_stdio_material(row)
    try:
        proc = await asyncio.create_subprocess_exec(
            material["command"],
            *material["args"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **material["env"]},
            limit=max_output_bytes + _STDIO_STREAM_LIMIT_MARGIN,
        )
    except (OSError, ValueError):
        raise _discover_failed() from None
    try:
        raw_tools = await asyncio.wait_for(
            _stdio_handshake(proc, max_output_bytes=max_output_bytes), timeout
        )
    except asyncio.TimeoutError:
        raise _discover_failed() from None
    except (OSError, ValueError):
        raise _discover_failed() from None
    finally:
        await _terminate_stdio_process(proc)
    if len(raw_tools) > _MAX_TOOLS_PER_SERVER:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "MCP_DISCOVER_FAILED",
                "message": f"工具数量超过上限（{_MAX_TOOLS_PER_SERVER}）",
            },
        )
    return _normalize_tools(raw_tools)


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
    """发现链（限流在路由层）：404 门 → stdio：subprocess 直拉 + stdio 握手
    （discover_stdio，M3）/ http：出网前 SSRF 公网校验（net_guard，provider
    连通性测试同款）→ 握手拉 tools → owner 事务内先删后插缓存（幂等）+ 同事务
    审计。子进程/网络请求均不进 owner 事务（provider test 同款——读出行后即关
    会话，写回前二次 404 门；expire_on_commit=False 保证脱离会话的行属性可读）。
    """
    async with owner_session(runtime, user_id) as db:
        row = await _get_server_row(db, server_id)
        is_stdio = row.transport_kind == "stdio"
        url = None if is_stdio else row.url

    if is_stdio:
        tools = await discover_stdio(row)
    else:
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


# ---------------------------------------------------------------------------
# 任务快照挂载（Phase 10 M4）：mcp_refs 校验与冻结描述符
# ---------------------------------------------------------------------------

MAX_MCP_REFS_PER_TASK = 3  # 单任务挂载上限（Phase 10 设计 §二：≤3）


async def snapshot_refs_for_task(
    db: AsyncSession, mcp_refs: list[dict] | None
) -> list[dict] | None:
    """任务创建载荷 mcp_refs 的校验与快照冻结（调用方 owner 事务内执行）。

    门序：≤3 计数门（400）→ 逐项 server_id UUID 门（400）→ 去重保序 →
    owner RLS 圈定取行（缺失/跨用户统一 404，与 server 面 NOT_FOUND 同形）→
    enabled 门（挂载已停用 server 400）→ 冻结描述符
    ``[{server_id, name, transport_kind}]``。

    冻结语义：描述符进任务快照后任务期不可变——PUT（改名/停用）/删除 server
    不回写任务（执行期缺失/停用静默剔除，kill switch 联动属 M7）。命令材料
    不进快照（信封密文只存 user_mcp_servers，执行期按 server_id 解引用解封）。
    None/空列表 → None（tasks.mcp_servers 存 NULL，视图归一为空列表）。
    """
    if not mcp_refs:
        return None
    if len(mcp_refs) > MAX_MCP_REFS_PER_TASK:
        raise _validation(f"mcp_refs 最多挂载 {MAX_MCP_REFS_PER_TASK} 个 MCP server")
    ids: list[_uuid.UUID] = []
    for ref in mcp_refs:
        raw = (ref or {}).get("server_id")
        try:
            sid = _uuid.UUID(str(raw))
        except (AttributeError, TypeError, ValueError) as exc:
            raise _validation("mcp_refs[].server_id 不是合法 UUID") from exc
        if sid not in ids:
            ids.append(sid)
    rows = {
        row.id: row
        for row in (
            await db.execute(select(UserMcpServer).where(UserMcpServer.id.in_(ids)))
        ).scalars()
    }
    frozen: list[dict] = []
    for sid in ids:
        row = rows.get(sid)
        if row is None:  # 缺失/跨用户（RLS 0 行）统一 404
            raise _server_not_found()
        if not row.enabled:
            raise _validation("不能挂载已停用的 MCP server")
        frozen.append(
            {"server_id": str(row.id), "name": row.name, "transport_kind": row.transport_kind}
        )
    return frozen
