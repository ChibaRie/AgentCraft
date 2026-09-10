"""V2 BYOK Provider 服务域（PlanD-T6 起）。

契约出处：Supplement §3（目录只读面 / CRUD / 连通性测试）、Database Design §3
（owner-RLS、软撤联动）、app-layer design §4.2（KeySealer）。本模块纪律：

- 一切 owner 读写要求调用方已置 GUC（owner_session 事务内）；本模块不 commit；
- 统一 404：行缺失/revoked 一律 HTTPException NOT_FOUND（跨用户 RLS 静默 0 行同形）；
- Key 材料红线：明文/密文/DEK 不进日志、错误消息、幂等记录。
"""

import logging
import uuid as _uuid
from dataclasses import dataclass

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
        if not catalog.enabled:
            raise AgentCraftError(
                ErrorCode.CATALOG_ITEM_DISABLED, "目录条目已停用", http_status=400
            )
        if updates["model_id"] not in list(catalog.models):
            raise AgentCraftError(
                ErrorCode.MODEL_NOT_ALLOWED, "模型不在目录白名单", http_status=400
            )
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
