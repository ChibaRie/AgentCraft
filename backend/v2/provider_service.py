"""V2 BYOK Provider 服务域（PlanD-T6 起）。

契约出处：Supplement §3（目录只读面 / CRUD / 连通性测试）、Database Design §3
（owner-RLS、软撤联动）、app-layer design §4.2（KeySealer）。本模块纪律：

- 一切 owner 读写要求调用方已置 GUC（owner_session 事务内）；本模块不 commit；
- 统一 404：行缺失/revoked 一律 HTTPException NOT_FOUND（跨用户 RLS 静默 0 行同形）；
- Key 材料红线：明文/密文/DEK 不进日志、错误消息、幂等记录。
"""

import json
import logging
import time
import uuid as _uuid
from dataclasses import dataclass

import httpx
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.idempotency import begin, store, subject_user
from backend.v2.ids import uuid7
from backend.v2.models import ProviderCatalog, UserProvider
from backend.v2.models.tasking import TaskEvent
from backend.v2.provider_crypto import key_sealer
from backend.v2.runtime import V2Runtime, owner_session

logger = logging.getLogger("agentcraft.provider")

_NOT_FOUND_DETAIL = {"code": "NOT_FOUND", "message": "资源不存在"}


def _provider_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)


def _assert_catalog_usable(catalog: ProviderCatalog, model_id: str | None = None) -> None:
    """目录可用门（create/test/resolve 三处共用；D11/D17）：enabled 门恒查；
    model_id 给定时加白名单门。test 路径不传 model_id——D10 连通性测试不做
    白名单复验（存量行的模型允许目录白名单收缩后仍可测试）。"""
    if not catalog.enabled:
        raise AgentCraftError(ErrorCode.CATALOG_ITEM_DISABLED, "目录条目已停用", http_status=400)
    if model_id is not None and model_id not in list(catalog.models):
        raise AgentCraftError(ErrorCode.MODEL_NOT_ALLOWED, "模型不在目录白名单", http_status=400)


def _out(row: UserProvider, catalog_display_name: str) -> dict:
    """ORM 行 → ProviderOut 形态 dict（裁决 D13 字段清单；key_last4 裸 4 字符）。"""
    return {
        "id": str(row.id),
        "catalog_id": str(row.catalog_id),
        "catalog_display_name": catalog_display_name,
        "model_id": row.model_id,
        "key_last4": row.key_last4,
        "key_version": row.key_version,
        "status": row.status,
        "is_default": row.is_default,
        "created_at": row.created_at.isoformat(),
    }


async def list_catalog(db: AsyncSession) -> list[dict]:
    """启用中的目录条目（服务层过滤——provider_catalog 无 RLS，D11/D13）。"""
    rows = (
        (
            await db.execute(
                select(ProviderCatalog)
                .where(ProviderCatalog.enabled.is_(True))
                .order_by(ProviderCatalog.display_name.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "display_name": r.display_name,
            "allowed_host": r.allowed_host,
            "models": list(r.models),
        }
        for r in rows
    ]


async def list_user_providers(db: AsyncSession) -> list[dict]:
    """owner 事务内列当前用户 active Provider（RLS 限定；revoked 不可见，D13）。"""
    rows = (
        await db.execute(
            select(UserProvider, ProviderCatalog.display_name)
            .join(ProviderCatalog, UserProvider.catalog_id == ProviderCatalog.id)
            .where(UserProvider.status == "active")
            .order_by(UserProvider.created_at.asc())
        )
    ).all()
    return [_out(row, display_name) for row, display_name in rows]


async def get_provider_row(db: AsyncSession, provider_id: str) -> UserProvider:
    """owner 事务内取 active 行：格式非法 → 400（与 D17/create 同形，防裸 ValueError
    落 500 兜底）；缺失/revoked → 统一 404（D13；跨用户 RLS 0 行同形）。"""
    try:
        pid = _uuid.UUID(provider_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "provider_id 不是合法 UUID"},
        ) from exc
    row = (
        await db.execute(
            select(UserProvider).where(
                UserProvider.id == pid,
                UserProvider.status == "active",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _provider_not_found()
    return row


async def get_provider_detail(db: AsyncSession, provider_id: str) -> dict:
    """owner 事务内单行视图（T7/T9 响应复用）。"""
    row = await get_provider_row(db, provider_id)
    catalog = (
        await db.execute(select(ProviderCatalog).where(ProviderCatalog.id == row.catalog_id))
    ).scalar_one()
    return _out(row, catalog.display_name)


@dataclass(frozen=True)
class ResolvedProvider:
    """任务创建的 Provider 解析产物（裁决 D17；Phase 6 复用接口冻结）。"""

    provider_id: str
    catalog_id: str
    model_id: str
    key_version: int


async def _resolve_default_row(db: AsyncSession, user_id: str) -> UserProvider:
    """默认位 active 行（D17：WHERE user_id + is_default + status='active'，RLS 限定）；
    无 → PROVIDER_NOT_CONFIGURED 400。"""
    row = (
        await db.execute(
            select(UserProvider).where(
                UserProvider.user_id == _uuid.UUID(user_id),
                UserProvider.is_default.is_(True),
                UserProvider.status == "active",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AgentCraftError(
            ErrorCode.PROVIDER_NOT_CONFIGURED, "未配置有效 Provider", http_status=400
        )
    return row


async def resolve_task_provider(
    db: AsyncSession, *, user_id: str, provider_id: str | None
) -> ResolvedProvider:
    """任务创建的 Provider 解析（裁决 D17；Phase 6 复用接口冻结）。

    判定链：provider_id 给定 → UUID/active 行门（get_provider_row：非法 400 /
    缺失、revoked 404）；None → 默认位 active 行（无 → PROVIDER_NOT_CONFIGURED
    400）。两路同链复验目录可用性（enabled → 白名单）。调用方传入的 db 必须已
    设 GUC（owner_session / Phase 6 创建事务）；本函数不 commit。
    """
    if provider_id is not None:
        row = await get_provider_row(db, provider_id)
    else:
        row = await _resolve_default_row(db, user_id)
    catalog = (
        await db.execute(select(ProviderCatalog).where(ProviderCatalog.id == row.catalog_id))
    ).scalar_one()
    _assert_catalog_usable(catalog, row.model_id)
    return ResolvedProvider(
        provider_id=str(row.id),
        catalog_id=str(row.catalog_id),
        model_id=row.model_id,
        key_version=row.key_version,
    )


ROUTE_CREATE = "/api/v2/providers"

_ACTIVE_ENTRY_UQ = "uq_user_providers_active_entry"
_ONE_DEFAULT_UQ = "uq_user_providers_one_default"

_DUPLICATE_MESSAGE = "已存在相同目录与模型的 Provider"
_DEFAULT_CONFLICT_MESSAGE = "默认 Provider 设置冲突，请重试"


@dataclass(frozen=True)
class Replay:
    """幂等重放载荷（§7 重放优先；端点原样返回，不携带 Set-Cookie）。"""

    status_code: int
    response_json: dict


def _duplicate(message: str) -> AgentCraftError:
    return AgentCraftError(ErrorCode.PROVIDER_DUPLICATE, message, http_status=409)


def _map_integrity_conflict(exc: IntegrityError) -> AgentCraftError:
    """唯一冲突按约束名分流（D4/D5）：默认互斥 vs 活跃条目重复。"""
    if _ONE_DEFAULT_UQ in str(exc):
        return _duplicate(_DEFAULT_CONFLICT_MESSAGE)
    return _duplicate(_DUPLICATE_MESSAGE)


async def create_provider(
    runtime: V2Runtime,
    *,
    user_id: str,
    updates: dict,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """创建 BYOK Provider（裁决 D4/D5/D7/D11）。

    门序：幂等 begin（app 裸会话，route=ROUTE_CREATE）→ owner_session 单事务
    （UUID/目录边界 → enabled 门 → 白名单门 → 重复检查 → 默认互斥 → seal 落库
    → store）。updates 为 ProviderCreateRequest.model_dump(exclude_unset=True)。
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

    try:
        cid = _uuid.UUID(updates["catalog_id"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_ERROR", "message": "catalog_id 不是合法 UUID"},
        ) from exc

    async with owner_session(runtime, user_id) as db:
        catalog = (
            await db.execute(select(ProviderCatalog).where(ProviderCatalog.id == cid))
        ).scalar_one_or_none()
        if catalog is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": "目录条目不存在"},
            )
        _assert_catalog_usable(catalog, updates["model_id"])
        dup = (
            await db.execute(
                select(UserProvider.id).where(
                    UserProvider.user_id == _uuid.UUID(user_id),
                    UserProvider.catalog_id == cid,
                    UserProvider.model_id == updates["model_id"],
                    UserProvider.status == "active",
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise _duplicate(_DUPLICATE_MESSAGE)
        if updates.get("is_default") is True:
            await db.execute(
                text(
                    "UPDATE user_providers SET is_default = false "
                    "WHERE user_id = :u AND is_default = true"
                ),
                {"u": user_id},
            )
        new_id = uuid7()
        key_ciphertext, dek_wrapped = key_sealer().seal(updates["api_key"], provider_id=str(new_id))
        db.add(
            UserProvider(
                id=new_id,
                user_id=_uuid.UUID(user_id),
                catalog_id=cid,
                model_id=updates["model_id"],
                key_ciphertext=key_ciphertext,
                dek_wrapped=dek_wrapped,
                key_last4=updates["api_key"][-4:],  # D7：末 4 位原样
                key_version=1,
                status="active",
                is_default=bool(updates.get("is_default")),
            )
        )
        try:
            await db.flush()
        except IntegrityError as exc:
            raise _map_integrity_conflict(exc) from exc
        detail = await get_provider_detail(db, str(new_id))
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


_UNSTARTED_STATES_SQL = "('uploading','queued','ready')"  # 裁决 D3：无活跃轮三态


async def _release_task_holdings(db: AsyncSession, task_id: str, owner_id: str) -> int:
    """对称释放（D3/DB Design §4.2:147）：该任务 state='held' 的 reservation 置
    released（uploading 持 active；queued/ready 另持 task_root），按释放的
    kind='active' 条数递减 user_quota_usage.active_tasks（WHERE state='held'
    保证幂等不重复递减；task_root 不计 active_tasks；任务根存储清理归
    Phase 6 TERMINATE_TASKS_HOOK）。返回递减量。
    """
    released_kinds = (
        (
            await db.execute(
                text(
                    "UPDATE task_reservations SET state = 'released' "
                    "WHERE task_id = :i AND state = 'held' RETURNING kind"
                ),
                {"i": task_id},
            )
        )
        .scalars()
        .all()
    )
    active_released = sum(1 for kind in released_kinds if kind == "active")
    if active_released:
        await db.execute(
            text("UPDATE user_quota_usage SET active_tasks = active_tasks - :n WHERE user_id = :u"),
            {"n": active_released, "u": owner_id},
        )
    return active_released


async def fail_unstarted_tasks(
    db: AsyncSession, *, provider_id: str, reason: str = "provider_key_revoked"
) -> int:
    """撤销/轮换联动：该 Provider 的未开始任务批量终态化（裁决 D3）。

    契约（DB Design §4.2:147 终态强制；语义模板 = Sup §1.2 abort 行 + §8
    uploading TTL 行）：在调用方（revoke_provider / update_provider 轮换）事务内
    执行，本函数不 commit；返回终态化任务数。逐任务：FOR UPDATE 锁定 →
    tasks（failed + abort_reason + event_sequence 递增）→ _release_task_holdings
    对称释放 → queued/ready 的 task_rounds(pending) 置 cancelled 并补
    round_cancelled 事件（防 Phase 6 dispatcher SKIP LOCKED 领取已 failed 任务
    的僵尸轮）→ task_events(status_changed) 行 sequence 同步递增。running 不在
    此列（Phase 6 settle 路径比对 key_version，KEY_VERSION_REVOKED 已注册）。
    """
    rows = (
        (
            await db.execute(
                text(
                    "SELECT id, owner_id, event_sequence FROM tasks "
                    f"WHERE provider_id = :p AND status IN {_UNSTARTED_STATES_SQL} FOR UPDATE"
                ),
                {"p": provider_id},
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        await _release_task_holdings(db, row["id"], row["owner_id"])
        round_cancelled = (
            await db.execute(
                text(
                    "UPDATE task_rounds SET state = 'cancelled' "
                    "WHERE task_id = :i AND state = 'pending'"
                ),
                {"i": row["id"]},
            )
        ).rowcount
        seq = row["event_sequence"] + 1
        await db.execute(
            text(
                "UPDATE tasks SET status = 'failed', abort_reason = :r, "
                "event_sequence = event_sequence + :step WHERE id = :i"
            ),
            {"r": reason, "step": 1 + (1 if round_cancelled else 0), "i": row["id"]},
        )
        db.add(
            TaskEvent(
                task_id=row["id"],
                owner_id=row["owner_id"],
                sequence=seq,
                type="status_changed",
                payload_json={"status": "failed", "reason": reason},
            )
        )
        if round_cancelled:
            db.add(
                TaskEvent(
                    task_id=row["id"],
                    owner_id=row["owner_id"],
                    sequence=seq + 1,
                    type="round_cancelled",
                    payload_json={"reason": reason},
                )
            )
    return len(rows)


_NULL_API_KEY_MESSAGE = "api_key 不支持置空（两态语义：缺席=不变，字符串=替换）"


async def update_provider(
    runtime: V2Runtime,
    *,
    user_id: str,
    provider_id: str,
    updates: dict,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """更新（裁决 D2/D5/D11/D14）：api_key 两态轮换 / is_default 互斥 / model 白名单。

    门序同 create：幂等 begin（route=具体路径）→ owner 事务（active 行 404 门 →
    显式 null 400 → model 白名单 → 轮换 seal+version+1+联动 / 默认互斥 → store）。
    model_id 变更与目录禁用正交（D11：PUT 允许）；白名单按原目录 models 校验。
    updates 为 ProviderUpdateRequest.model_dump(exclude_unset=True)。
    """
    route = f"/api/v2/providers/{provider_id}"
    async with runtime.app_factory() as db:
        replay = await begin(
            db, subject_hash=subject_user(user_id), route=route, key=idem_key, req_hash=idem_hash
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])

    async with owner_session(runtime, user_id) as db:
        row = await get_provider_row(db, provider_id)  # 缺失/revoked → 404（跨用户同形）
        if "api_key" in updates and updates["api_key"] is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "VALIDATION_ERROR", "message": _NULL_API_KEY_MESSAGE},
            )
        if "model_id" in updates and updates["model_id"] != row.model_id:
            catalog = (
                await db.execute(
                    select(ProviderCatalog).where(ProviderCatalog.id == row.catalog_id)
                )
            ).scalar_one()
            if updates["model_id"] not in list(catalog.models):
                raise AgentCraftError(
                    ErrorCode.MODEL_NOT_ALLOWED, "模型不在目录白名单", http_status=400
                )
            row.model_id = updates["model_id"]
        if "api_key" in updates and isinstance(updates["api_key"], str):
            key_ciphertext, dek_wrapped = key_sealer().seal(
                updates["api_key"], provider_id=str(row.id)
            )
            row.key_ciphertext = key_ciphertext
            row.dek_wrapped = dek_wrapped
            row.key_last4 = updates["api_key"][-4:]
            row.key_version += 1
            await fail_unstarted_tasks(db, provider_id=str(row.id))  # D3 同事务
        if updates.get("is_default") is True:
            await db.execute(
                text(
                    "UPDATE user_providers SET is_default = false "
                    "WHERE user_id = :u AND is_default = true AND id <> :e"
                ),
                {"u": user_id, "e": row.id},
            )
            row.is_default = True
        elif updates.get("is_default") is False:
            row.is_default = False
        try:
            await db.flush()
        except IntegrityError as exc:
            raise _map_integrity_conflict(exc) from exc
        catalog_name = (
            await db.execute(
                select(ProviderCatalog.display_name).where(ProviderCatalog.id == row.catalog_id)
            )
        ).scalar_one()
        detail = _out(row, catalog_name)
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


async def revoke_provider(
    runtime: V2Runtime,
    *,
    user_id: str,
    provider_id: str,
    idem_key: str,
    idem_hash: str,
) -> "dict | Replay":
    """软撤（裁决 D3/D6）：status→revoked + 默认清除 + 联动，同事务；幂等。

    DELETE 无请求体：request_hash(None)。幂等 begin 先于 404 门——同 key 的
    DELETE 重放在行已 revoked 后仍原样重放 200（§7 重放优先）。
    """
    route = f"/api/v2/providers/{provider_id}"
    async with runtime.app_factory() as db:
        replay = await begin(
            db, subject_hash=subject_user(user_id), route=route, key=idem_key, req_hash=idem_hash
        )
    if replay is not None:
        return Replay(replay["status_code"], replay["response_json"])

    async with owner_session(runtime, user_id) as db:
        row = await get_provider_row(db, provider_id)  # 404 门（重放之后）
        row.status = "revoked"
        row.is_default = False  # D6：唯一索引不含 status 谓词，不清除会阻塞新默认
        await fail_unstarted_tasks(db, provider_id=str(row.id))
        await db.flush()
        body = {"data": {"id": str(row.id), "status": "revoked"}}
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


_TEST_TIMEOUT = httpx.Timeout(90.0, connect=5.0)  # D10：连接 5s / 总 90s
_MAX_TEST_RESPONSE_BYTES = 2 * 1024 * 1024  # D10：2MB 流式硬上限
_TEST_URL_TEMPLATE = "https://{host}{path}"  # D10：不叠 path_prefix


def _count_models(body: bytes) -> int:
    """models_visible：顶层 data/models 数组长度；顶层为数组取其长度；解析失败 → 0。"""
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return 0
    if isinstance(parsed, list):
        return len(parsed)
    if isinstance(parsed, dict):
        for key in ("data", "models"):
            value = parsed.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


async def test_provider_connectivity(
    runtime: V2Runtime,
    *,
    user_id: str,
    provider_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """连通性测试（Sup §3；裁决 D10/D11）。

    open() 是 Phase 3 内明文 Key 的唯一解密消费点（control grant 属 Phase 6）：
    明文仅存活于本协程内存，禁缓存/禁日志/禁入错误消息。URL 仅由 catalog 行拼装；
    httpx follow_redirects=False + trust_env=False；流式 2MB 上限；响应恰
    {ok, latency_ms, models_visible}，不回传上游任何内容（Eng §6 红线）。
    """
    async with owner_session(runtime, user_id) as db:
        row = await get_provider_row(db, provider_id)  # 缺失/revoked → 404
        catalog = (
            await db.execute(select(ProviderCatalog).where(ProviderCatalog.id == row.catalog_id))
        ).scalar_one()
        _assert_catalog_usable(catalog)  # D10：仅 enabled 门，不做白名单复验
        key_ciphertext, dek_wrapped, aad_pid = row.key_ciphertext, row.dek_wrapped, str(row.id)
        method = catalog.healthcheck_method
        url = _TEST_URL_TEMPLATE.format(host=catalog.allowed_host, path=catalog.healthcheck_path)

    plaintext_key = key_sealer().open(key_ciphertext, dek_wrapped, provider_id=aad_pid)
    started = time.perf_counter()
    ok = False
    models_visible = 0
    try:
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False, timeout=_TEST_TIMEOUT
        ) as client:
            async with client.stream(
                method,
                url,
                headers={"authorization": f"Bearer {plaintext_key}", "accept": "application/json"},
            ) as resp:
                total = 0
                chunks: list[bytes] = []
                oversized = False
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_TEST_RESPONSE_BYTES:
                        oversized = True
                        break
                    chunks.append(chunk)
                if not oversized:
                    ok = 200 <= resp.status_code < 300
                    if ok:
                        models_visible = _count_models(b"".join(chunks))
    except httpx.HTTPError:
        ok = False  # 超时/连接失败/流错误 → 统一失败形态；异常不外传不落日志
    latency_ms = max(0, int((time.perf_counter() - started) * 1000))
    return {"ok": ok, "latency_ms": latency_ms, "models_visible": models_visible}
