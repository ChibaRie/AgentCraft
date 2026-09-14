"""V2 产物登记管道（Phase 6 T7，D2 全回调）：容器回调直写控制面存储。

契约出处：Phase 6 计划 T7/D2/D16（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）、
Sup §4:111（输入+产物总量 10 MiB）、DB §3:109（storage_key 逻辑键契约）。模块纪律
（T3/T4 同型）：

- **服务函数不 begin 不 commit**：事务由调用方收口（回调端点 owner_session /
  测试 owner_tx）；调用方会话必须已 set_current_owner（GUC 事务本地，RLS 生效
  前提），本模块不重复设置；
- **写路径自行锁任务行 + 条件仲裁（形态参照 T4 upload）**：``_lock_task`` FOR
  UPDATE（缺失/他人/已删除统一 TASK_NOT_FOUND 404）→ 轮面 fence 条件 UPDATE
  （``WHERE id AND lease_epoch AND state='running'``，rowcount=0 →
  TOOL_CALL_REJECTED 400——与执行器写侧围栏同型：轮收口/被 fence 后写入拒绝）；
- 文件名全集校验复用 T4 ``_validate_file_name``（控制字符/路径分隔符/`.`/`..`
  /超 255——**直接 import 消费，不复制漂移**）；单文件 5 MiB、输入+产物总量
  10 MiB（FILE_LIMIT_EXCEEDED 400；总量口径与 T4 一致：input+output 全部存活
  行，覆写按净值扣减将被墓碑化的旧行）；
- **upsert**（D2：产物=回调直写 + 事务化覆写）：NFC 规范化同名已 registered →
  旧行 deleted（条件 UPDATE RETURNING 闸门）+ 旧行 artifact_copy reservation
  released（RETURNING bytes 闸门）+ 存储账退还（下限谓词，task_release 同型）
  + 旧物理文件删除；新行 registered（produced_in_round_id 钉来源轮）+ 新
  artifact_copy reservation（per (task_id,file_id) 部分唯一索引
  one_live_artifact_copy_reservation）+ 用户/平台存储账条件增（上限谓词，
  rowcount=0 → QUOTA_STORAGE_EXCEEDED 429）——账随 reservation 走，+1/-1 严格
  对称（释放面 task_release 以同一 reservation 行为闸，无双重退还）；
- 物理双写 outputs/<fid> + artifacts/<task_id>/<fid>（D10 布局），时序：全部
  DB 门与账落定 → flush → 新文件双写（任一失败补偿 unlink）→ 旧文件 unlink；
  任何异常上抛由调用方回滚（行/账随事务撤销），账实一致；
- 物理路径只认行内 storage_key 严格解析派生（T4 防穿越纪律顺延），file_name
  永不参与路径拼接。

list_artifacts / resolve_download 为 T8a 产物列表与下载路由的服务消费面；
register_output 为 /internal/tools/write-output-file 回调消费面（D16 owner
会话内调用，GUC 已设）。
"""

import hashlib
import logging
import urllib.parse
import uuid as _uuid
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.errors import AgentCraftError, ErrorCode
from backend.v2.ids import uuid7
from backend.v2.models import Task, TaskFile, TaskReservation
from backend.v2.task_file_service import (
    _MAX_SINGLE_FILE_BYTES,
    _nfc,
    _validate_file_name,
)
from backend.v2.task_file_service import (
    _MAX_TASK_INPUT_BYTES as _MAX_TOTAL_BYTES,
)
from backend.v2.task_release import _refund_storage
from backend.v2.task_service import _lock_task
from backend.v2.task_views import _parse_id, _task_not_found

__all__ = [
    "download_headers",
    "list_artifacts",
    "register_output",
    "resolve_download",
]

logger = logging.getLogger("agentcraft.task.artifacts")


def _tool_call_rejected(message: str) -> AgentCraftError:
    return AgentCraftError(ErrorCode.TOOL_CALL_REJECTED, message, http_status=400)


def _file_not_found() -> AgentCraftError:
    return AgentCraftError(ErrorCode.FILE_NOT_FOUND, "文件不存在", http_status=404)


# 轮面 fence（T7 写路径条件仲裁；与执行器 _FENCE_ROUND_SQL 同型 no-op 推进式
# UPDATE——行锁随事务持有，rowcount=0 即轮已收口/被 fence）
_FENCE_ROUND_SQL = text(
    "UPDATE task_rounds SET lease_expires_at = lease_expires_at "
    "WHERE id = :rid AND lease_epoch = :epoch AND state = 'running'"
)

# 用户/平台存储账条件增（commit_input 同型上限谓词；rowcount=0 → 429 整事务回滚）
_CHARGE_USER_SQL = text(
    "UPDATE user_quota_usage SET retained_storage_bytes = retained_storage_bytes + :n "
    "WHERE user_id = :u AND retained_storage_bytes + :n <= "
    "(SELECT max_retained_storage_bytes FROM user_quotas WHERE user_id = :u)"
)
_CHARGE_PLATFORM_SQL = text(
    "UPDATE platform_storage SET retained_storage_bytes = retained_storage_bytes + :n "
    "WHERE singleton AND retained_storage_bytes + :n <= max_retained_storage_bytes"
)


def _quota_storage_exceeded() -> AgentCraftError:
    return AgentCraftError(ErrorCode.QUOTA_STORAGE_EXCEEDED, "保留存储配额不足", http_status=429)


def _output_paths_from_key(storage, storage_key: str) -> tuple[Path, Path] | None:
    """行内 storage_key → (outputs 路径, artifacts 副本路径)（防穿越唯一通道）：
    tasks/<uuid>/<uuid> 严格解析，形态不符（含段非 UUID）→ None。"""
    parts = storage_key.split("/")
    if len(parts) != 3 or parts[0] != "tasks":
        return None
    try:
        _uuid.UUID(parts[1])
        _uuid.UUID(parts[2])
    except ValueError:
        return None
    task_part, file_part = parts[1], parts[2]
    return storage.output_path(task_part, file_part), storage.artifact_path(task_part, file_part)


async def _fence_round(db: AsyncSession, round_uuid, lease_epoch: int) -> None:
    """轮面 fence 条件仲裁：no-op 推进式 UPDATE（行锁随事务持有），rowcount=0
    即轮已收口/被 fence → TOOL_CALL_REJECTED 400（执行器写侧围栏同型）。"""
    fenced = await db.execute(_FENCE_ROUND_SQL, {"rid": round_uuid, "epoch": int(lease_epoch)})
    if fenced.rowcount == 0:
        raise _tool_call_rejected("产物写入窗口已关闭（轮已收口或 lease 已变更）")


def _file_limit(message: str) -> AgentCraftError:
    return AgentCraftError(ErrorCode.FILE_LIMIT_EXCEEDED, message, http_status=400)


def _assert_size_limits(content: bytes, total_bytes: int) -> None:
    """单文件 5 MiB + 输入+产物总量 10 MiB（FILE_LIMIT_EXCEEDED 400；
    total_bytes 为存活行净值——覆写场景已扣减将被墓碑化的旧行）。"""
    if len(content) > _MAX_SINGLE_FILE_BYTES:
        raise _file_limit(f"单个文件超出 {_MAX_SINGLE_FILE_BYTES} 字节（5 MiB）上限")
    if total_bytes + len(content) > _MAX_TOTAL_BYTES:
        raise _file_limit(f"输入与产物总量超出 {_MAX_TOTAL_BYTES} 字节（10 MiB）上限")


async def _scan_live_rows(db: AsyncSession, task_id) -> list:
    """存活文件行（墓碑不计；created_at ASC + id ASC 稳定序）——总量口径与
    NFC 同名 old-row 定位的单一扫描面。"""
    return list(
        (
            await db.execute(
                select(TaskFile)
                .where(TaskFile.task_id == task_id, TaskFile.state != "deleted")
                .order_by(TaskFile.created_at.asc(), TaskFile.id.asc())
            )
        ).scalars()
    )


def _find_old_output_row(live: list, file_name: str):
    """NFC 规范化同名的存活产物行（upsert 覆写对象；无则 None）。"""
    return next(
        (r for r in live if r.direction == "output" and _nfc(r.file_name) == _nfc(file_name)),
        None,
    )


async def _retire_old_row(db: AsyncSession, task, old, owner_uuid) -> None:
    """upsert 旧行清退：墓碑（条件 UPDATE RETURNING 闸门，任务行锁在握零行
    理论不可达——双保险）+ artifact_copy reservation released（RETURNING bytes
    闸门）+ 存储账退还（下限谓词）。账随 reservation 行走，与释放面无双重退还。
    （带 RETURNING 的 ORM DML 结果为 ChunkedIteratorResult：闸门以返回行判定，
    非 rowcount——task_release._release_reservations 同型。）"""
    tomb = await db.execute(
        update(TaskFile)
        .where(TaskFile.id == old.id, TaskFile.state == "registered")
        .values(state="deleted")
        .returning(TaskFile.id)
        .execution_options(synchronize_session=False)
    )
    if tomb.first() is None:
        raise _file_not_found()
    released = await db.execute(
        update(TaskReservation)
        .where(
            TaskReservation.task_id == task.id,
            TaskReservation.file_id == old.id,
            TaskReservation.kind == "artifact_copy",
            TaskReservation.state == "held",
        )
        .values(state="released")
        .returning(TaskReservation.bytes)
        .execution_options(synchronize_session=False)
    )
    refunded = sum(int(n or 0) for n in released.scalars())
    if refunded:
        await _refund_storage(db, owner_uuid, refunded)


async def _insert_registered_row(
    db: AsyncSession, task, *, fid, file_name: str, content: bytes, round_uuid
) -> None:
    """新产物行（registered）+ artifact_copy reservation（per (task_id,file_id)
    部分唯一索引 one_live_artifact_copy_reservation）。"""
    db.add(
        TaskFile(
            id=fid,
            task_id=task.id,
            owner_id=task.owner_id,
            direction="output",
            file_name=file_name,
            storage_key=f"tasks/{task.id}/{fid}",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            state="registered",
            produced_in_round_id=round_uuid,
        )
    )
    db.add(
        TaskReservation(
            id=uuid7(),
            task_id=task.id,
            user_id=task.owner_id,
            kind="artifact_copy",
            file_id=fid,
            bytes=len(content),
            state="held",
        )
    )


async def _charge_storage(db: AsyncSession, owner_uuid, nbytes: int) -> None:
    """用户/平台存储账条件增（commit_input 同型上限谓词；任一 rowcount=0 →
    QUOTA_STORAGE_EXCEEDED 429，调用方事务整体回滚无半账）。"""
    user_inc = await db.execute(_CHARGE_USER_SQL, {"n": nbytes, "u": owner_uuid})
    if user_inc.rowcount == 0:
        raise _quota_storage_exceeded()
    platform_inc = await db.execute(_CHARGE_PLATFORM_SQL, {"n": nbytes})
    if platform_inc.rowcount == 0:
        raise _quota_storage_exceeded()


def _write_physical(storage, task_id, fid, content: bytes, old) -> None:
    """物理双写 outputs/<fid> + artifacts/<task_id>/<fid> + 旧文件清退（全部
    DB 门与账落定、flush 之后执行；任一失败补偿已写文件，异常上抛由调用方
    回滚——行/账随事务撤销，账实一致）。"""
    out_path = storage.output_path(str(task_id), str(fid))
    art_path = storage.artifact_path(str(task_id), str(fid))
    written: list[Path] = []
    try:
        out_path.write_bytes(content)
        written.append(out_path)
        art_path.write_bytes(content)
        written.append(art_path)
        if old is not None:
            old_paths = _output_paths_from_key(storage, old.storage_key)
            if old_paths is None:
                # storage_key 形态异常（服务端数据缺陷）：墓碑已落、旧物理文件
                # 留待 delete_task_storage 全树清扫，不阻塞本次合法写入
                logger.error(
                    "产物旧行 storage_key 形态异常（物理清退跳过）：file_id=%s key=%s",
                    old.id,
                    old.storage_key,
                )
            else:
                for path in old_paths:
                    path.unlink(missing_ok=True)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise


async def register_output(
    db: AsyncSession,
    storage,
    *,
    owner_id: str,
    task_id: str,
    file_name: str,
    content: bytes,
    round_id: str,
    lease_epoch: int,
) -> dict:
    """登记容器回调产物（D2 全回调写路径；D16 owner 会话内调用）。

    门序：task 锁（404）→ 轮面 fence（TOOL_CALL_REJECTED 400）→ 文件名全集
    校验（T4 函数）→ 单文件 5 MiB → 存活行扫描 + NFC 同名 old-row 定位 →
    总量 10 MiB（净值）→ upsert（旧行墓碑 + reservation released + 账退 +
    旧物理删）→ 新行 registered + reservation + 账增 → flush → 物理双写。

    返回 ``{"file": {id, file_name, sha256, size_bytes, state,
    produced_in_round_id}}``（路由层包 ``{data: ...}`` 信封）。
    """
    if not isinstance(content, bytes):
        raise _tool_call_rejected("文件内容必须为 bytes")
    task = await _lock_task(db, task_id)
    round_uuid = _parse_id(round_id, "round_id")
    owner_uuid = _parse_id(owner_id, "owner_id")
    await _fence_round(db, round_uuid, lease_epoch)
    _validate_file_name(file_name)
    live = await _scan_live_rows(db, task.id)
    old = _find_old_output_row(live, file_name)
    total_bytes = sum(int(r.size_bytes) for r in live)
    if old is not None:
        total_bytes -= int(old.size_bytes)  # 覆写净值：旧行将在本事务墓碑化
    _assert_size_limits(content, total_bytes)
    if old is not None:
        await _retire_old_row(db, task, old, owner_uuid)
    fid = uuid7()
    await _insert_registered_row(
        db, task, fid=fid, file_name=file_name, content=content, round_uuid=round_uuid
    )
    await _charge_storage(db, owner_uuid, len(content))
    await db.flush()
    _write_physical(storage, str(task.id), fid, content, old)
    return {
        "file": {
            "id": str(fid),
            "file_name": file_name,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "state": "registered",
            "produced_in_round_id": str(round_uuid),
        }
    }


async def list_artifacts(db: AsyncSession, *, owner_id: str, task_id: str) -> list[dict]:
    """产物列表（T8a GET /tasks/{id}/artifacts 服务面；Sup §4:103 同族形状）。

    仅存活产物（direction='output'、墓碑不可见）；含 produced_in_round_id（T7
    补列）；任务缺失/他人（RLS 0 行）/已删除统一 TASK_NOT_FOUND 404（Sup §7）。
    created_at ASC + id ASC 稳定序。
    """
    tid = _parse_id(task_id, "task_id")
    _parse_id(owner_id, "owner_id")
    task_exists = (
        await db.execute(select(Task.id).where(Task.id == tid, Task.status != "deleted"))
    ).scalar_one_or_none()
    if task_exists is None:
        raise _task_not_found()
    rows = (
        (
            await db.execute(
                select(TaskFile)
                .where(
                    TaskFile.task_id == tid,
                    TaskFile.direction == "output",
                    TaskFile.state != "deleted",
                )
                .order_by(TaskFile.created_at.asc(), TaskFile.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "file_name": r.file_name,
            "sha256": r.sha256,
            "size_bytes": int(r.size_bytes),
            "state": r.state,
            "produced_in_round_id": str(r.produced_in_round_id)
            if r.produced_in_round_id is not None
            else None,
        }
        for r in rows
    ]


async def resolve_download(
    db: AsyncSession, storage, *, owner_id: str, task_id: str, file_id: str
) -> tuple[Path, dict]:
    """产物下载解析（T8a GET /tasks/{id}/artifacts/{fid}/download 服务面）。

    direction='output' + state='registered' + 任务非 deleted（缺失/他人/已删除
    统一 TASK_NOT_FOUND 404；行不可见/非产物面/物理缺失 → FILE_NOT_FOUND 404）。
    返回 (物理路径, {file_name, sha256, size_bytes})——物理路径取 artifacts 登记副本
    （产物交付面）；outputs/<fid> 双写孪生仅供任务树视角。
    """
    tid = _parse_id(task_id, "task_id")
    _parse_id(owner_id, "owner_id")
    fid = _parse_id(file_id, "file_id")
    task_exists = (
        await db.execute(select(Task.id).where(Task.id == tid, Task.status != "deleted"))
    ).scalar_one_or_none()
    if task_exists is None:
        raise _task_not_found()
    row = (
        await db.execute(
            select(TaskFile).where(
                TaskFile.id == fid,
                TaskFile.task_id == tid,
                TaskFile.direction == "output",
                TaskFile.state == "registered",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _file_not_found()
    paths = _output_paths_from_key(storage, row.storage_key)
    if paths is None:
        raise _file_not_found()
    artifact_path = paths[1]
    if not artifact_path.is_file():
        raise _file_not_found()
    return artifact_path, {
        "file_name": row.file_name,
        "sha256": row.sha256,
        "size_bytes": int(row.size_bytes),
    }


def download_headers(file_name: str) -> dict[str, str]:
    """产物下载响应头（T8a FileResponse 消费）：attachment + nosniff。

    filename 提供 ASCII 兜底（非 ASCII 可打印字符/引号/反斜杠一律落 `_`），
    filename* 按 RFC 5987 携带 UTF-8 原名（非 ASCII 文件名不乱码）。
    """
    fallback = "".join(
        ch if ch.isascii() and ch.isprintable() and ch not in '\\"' else "_" for ch in file_name
    )
    return {
        "Content-Disposition": (
            f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{urllib.parse.quote(file_name, safe='')}"
        ),
        "X-Content-Type-Options": "nosniff",
    }
