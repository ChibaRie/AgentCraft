"""V2 任务文件输入面（Phase 6 T4）：staged 上传/列表/删除/读取元数据。

契约出处：Sup §4:109-111、Eng §5.2:130-136、DB §3:109、PRD §4.1:108-109、
Phase 6 计划 D7a/D16（docs/superpowers/plans/2026-09-14-v2-phase-6-task-domain.md）。
模块纪律（T3 同型）：

- **服务函数不 begin 不 commit**：事务由调用方收口（路由 owner_session / 测试
  owner_tx）；调用方会话必须已 set_current_owner（GUC 事务本地，RLS 生效前提），
  本模块不重复设置；
- 上传/删除先锁 task FOR UPDATE + 条件 UPDATE 仲裁（WHERE status='uploading'
  AND input_committed_at IS NULL，rowcount=0 → INPUT_COMMITTED 409——防与 commit
  竞态）；门分流与 commit_input 同型：冻结后 INPUT_COMMITTED 409、终态
  TASK_INVALID_TRANSITION 409、缺失/他人/已删除统一 TASK_NOT_FOUND 404；
- 限额三把门（PRD §4.1:108-109）：单任务输入≤10 个（存活 staged 存量+新增，
  deleted 墓碑不计）、单个≤5 MiB、输入+产物总量≤10 MiB（FILE_LIMIT_EXCEEDED
  400）；文件名非法/重复名 400 VALIDATION_ERROR；
- 文件名全集校验（Eng §5.2:133 照抄 + 空字节）：非空单段名——拒绝控制字符
  （Unicode Cc，含空字节与 DEL）、路径分隔符（正斜杠与反斜杠）、`.` 与 `..`、
  NFC 规范化后批内与存量重复名、超 255 字符（file_name VARCHAR(255) 边界）；
- 原子落盘时序（Eng §5.2:1）：sha256 → staging/<batch>/ 写入 → 全部成功 →
  Path.replace 原子 rename 进 tasks/<task_id>/inputs/<file_id> → INSERT
  task_files(direction='input', state='staged',
  storage_key='tasks/<task_id>/<file_id>')（DB §3:109 逻辑键契约）。rename 失败
  整批回退：INSERT 段未达（无行）+ 已 rename 目标与 staging 残留全清（无文件）；
- 防穿越（T3 审查顺延约束）：read_input_bytes 的 file_name 只作行查询键、永不
  参与路径拼接；物理路径只认行内 storage_key 严格解析派生（tasks/<uuid>/<uuid>
  → TaskStorage.input_path）；
- delete_file 物理删在墓碑 flush 后收尾（staged 文件无字节账——D7a 无回退分
  支）；unlink 异常上抛由调用方回滚（墓碑随事务撤销，账面一致）；无 storage
  形参（计划冻结签名），物理删根按 Settings 现读（与 v2_runtime_from_settings
  同一表达式，测试经 V2_TASK__STORAGE_ROOT env 钉 tmp 根）。

list_files / list_input_meta / read_input_bytes 为 T7 /internal/tools 回调与
T8a 路由消费面（回调按 D16 owner 会话内调用，GUC 已设）。
"""

import hashlib
import shutil
import unicodedata
import uuid as _uuid
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.errors import AgentCraftError, ErrorCode
from backend.v2.ids import uuid7
from backend.v2.models import Task, TaskFile
from backend.v2.task_service import _lock_task
from backend.v2.task_state import assert_transition
from backend.v2.task_storage import TaskStorage
from backend.v2.task_views import _parse_id, _task_not_found, _validation_error

__all__ = [
    "delete_file",
    "list_files",
    "list_input_meta",
    "read_input_bytes",
    "upload_files",
]

# PRD §4.1:108-109 上传限额（契约钉值；Settings.V2_TASK 无对应字段，不作配置面）
_MAX_FILES_PER_TASK = 10
_MAX_SINGLE_FILE_BYTES = 5 * 1024 * 1024
_MAX_TASK_INPUT_BYTES = 10 * 1024 * 1024
_FILE_NAME_MAX_CHARS = 255  # task_files.file_name VARCHAR(255) 边界


def _file_not_found() -> AgentCraftError:
    return AgentCraftError(ErrorCode.FILE_NOT_FOUND, "文件不存在", http_status=404)


def _input_committed() -> AgentCraftError:
    return AgentCraftError(ErrorCode.INPUT_COMMITTED, "任务输入已冻结", http_status=409)


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _validate_file_name(name: str) -> None:
    """单路径段全集校验（Eng §5.2:133 + 空字节）；违规 400 VALIDATION_ERROR。"""
    if not isinstance(name, str) or not name:
        raise _validation_error("文件名不能为空")
    if len(name) > _FILE_NAME_MAX_CHARS:
        raise _validation_error(f"文件名超出 {_FILE_NAME_MAX_CHARS} 字符")
    if any(unicodedata.category(ch) == "Cc" for ch in name):  # 控制字符（含空字节/DEL）
        raise _validation_error("文件名含控制字符")
    if "/" in name or "\\" in name:
        raise _validation_error("文件名含路径分隔符")
    if name in (".", ".."):
        raise _validation_error("文件名不能为 . 或 ..")


async def _lock_uploading_task(db: AsyncSession, task_id: str) -> Task:
    """锁 task FOR UPDATE + uploading 仲裁（上传/删除共用入口闸）。

    冻结后（queued/running/ready 或 input_committed_at 已设）INPUT_COMMITTED
    409；终态 assert_transition → TASK_INVALID_TRANSITION 409；缺失/他人/已删除
    由 _lock_task 统一 404。行锁在握后条件 UPDATE 双保险（rowcount=0 →
    INPUT_COMMITTED，理论不可达——与 T3 仲裁双保险同型）。
    """
    task = await _lock_task(db, task_id)
    if task.status != "uploading":
        if task.input_committed_at is not None or task.status in ("queued", "running", "ready"):
            raise _input_committed()
        assert_transition(task.status, "queued")
    arb = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.status == "uploading", Task.input_committed_at.is_(None))
        .values(status="uploading")  # 仲裁 no-op 写：PG 对同值 UPDATE 仍计 rowcount
        .execution_options(synchronize_session=False)
    )
    if arb.rowcount == 0:
        raise _input_committed()
    return task


def _settings_storage() -> TaskStorage:
    """delete_file 的物理删存储根（计划冻结签名无 storage 形参）：与
    v2_runtime_from_settings 同一表达式解析；get_settings 无缓存逐次现读 env，
    测试经 V2_TASK__STORAGE_ROOT 钉 tmp 根。"""
    settings = get_settings()
    return TaskStorage(settings.V2_TASK.storage_root or (settings.HOST_DATA_ROOT / "task-storage"))


def _input_path_from_key(storage: TaskStorage, storage_key: str) -> Path | None:
    """行内 storage_key → 物理路径（防穿越唯一通道）：tasks/<uuid>/<uuid> 严格
    解析，形态不符（含段非 UUID）→ None → 上层 404。"""
    parts = storage_key.split("/")
    if len(parts) != 3 or parts[0] != "tasks":
        return None
    try:
        _uuid.UUID(parts[1])
        _uuid.UUID(parts[2])
    except ValueError:
        return None
    return storage.input_path(parts[1], parts[2])


def _assert_upload_shape(uploads: list[tuple[str, bytes]]) -> None:
    """uploads 形状校验 + 逐项文件名全集校验（先于一切磁盘写与 DB 读）。"""
    if not isinstance(uploads, list) or not uploads:
        raise _validation_error("uploads 不能为空")
    for item in uploads:
        if not isinstance(item, tuple) or len(item) != 2:
            raise _validation_error("uploads 项必须为 (file_name, bytes) 二元组")
        name, content = item
        _validate_file_name(name)
        if not isinstance(content, bytes):
            raise _validation_error("文件内容必须为 bytes")


def _assert_name_uniqueness(live: list, uploads: list[tuple[str, bytes]]) -> None:
    """重复名门（Eng §5.2:133「规范化后重复名称」）：批内 NFC 互斥 + 对存量
    存活输入行（墓碑不计；input 方向本面口径）。"""
    batch_nfc = [_nfc(name) for name, _c in uploads]
    if len(set(batch_nfc)) != len(batch_nfc):
        raise _validation_error("上传批内存在重复文件名（规范化后）")
    existing_nfc = {_nfc(str(n)) for d, n, _s in live if d == "input"}
    if existing_nfc.intersection(batch_nfc):
        raise _validation_error("文件名与既有输入文件重复（规范化后）")


def _assert_upload_limits(live: list, uploads: list[tuple[str, bytes]]) -> None:
    """限额三把门（PRD §4.1:108-109）：单任务输入≤10 个（存量只计输入存活行）、
    单个≤5 MiB、输入+产物总量≤10 MiB（总量门含产物方向，Sup §4:111 口径）。"""
    input_count = sum(1 for d, _n, _s in live if d == "input")
    total_bytes = sum(int(size) for _d, _n, size in live)
    new_bytes = sum(len(content) for _n, content in uploads)
    if input_count + len(uploads) > _MAX_FILES_PER_TASK:
        raise AgentCraftError(
            ErrorCode.FILE_LIMIT_EXCEEDED,
            f"单任务输入文件数超上限（{_MAX_FILES_PER_TASK} 个）",
            http_status=400,
        )
    if any(len(content) > _MAX_SINGLE_FILE_BYTES for _n, content in uploads):
        raise AgentCraftError(
            ErrorCode.FILE_LIMIT_EXCEEDED,
            f"单个文件超出 {_MAX_SINGLE_FILE_BYTES} 字节（5 MiB）上限",
            http_status=400,
        )
    if total_bytes + new_bytes > _MAX_TASK_INPUT_BYTES:
        raise AgentCraftError(
            ErrorCode.FILE_LIMIT_EXCEEDED,
            f"输入与产物总量超出 {_MAX_TASK_INPUT_BYTES} 字节（10 MiB）上限",
            http_status=400,
        )


async def upload_files(
    db: AsyncSession,
    storage: TaskStorage,
    *,
    owner_id: str,
    task_id: str,
    uploads: list[tuple[str, bytes]],
) -> dict:
    """批量上传输入文件（staged；Eng §5.2:1-2 + Sup §4:109-111）。

    门序：task 锁与 uploading 仲裁 → uploads 形状校验 → 文件名全集校验（批内
    与存量 NFC 重复名）→ 限额三把门（存活行口径，墓碑不计）→ 逐文件 sha256 +
    staging/<batch>/ 写入 → 全部成功后逐个原子 rename 进 inputs/（任一失败整批
    回退：无行无文件）→ INSERT staged 行（契约时序，行最后落）。

    返回 ``{"files": [{id, file_name, sha256, size_bytes, state}]}``（state 恒
    'staged'；路由层包 201 信封）。
    """
    task = await _lock_uploading_task(db, task_id)
    _parse_id(owner_id, "owner_id")
    _assert_upload_shape(uploads)
    live = (
        await db.execute(
            select(TaskFile.direction, TaskFile.file_name, TaskFile.size_bytes).where(
                TaskFile.task_id == task.id, TaskFile.state != "deleted"
            )
        )
    ).all()
    _assert_name_uniqueness(live, uploads)
    _assert_upload_limits(live, uploads)

    batch = str(uuid7())
    staging_dir = storage.staging_dir(batch)
    prepared = []
    for name, content in uploads:
        fid = uuid7()
        prepared.append(
            {
                "id": fid,
                "file_name": name,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "tmp_path": staging_dir / str(fid),
                "target": storage.input_path(str(task.id), str(fid)),
                "payload": content,
            }
        )
    replaced: list[Path] = []
    try:
        for item in prepared:
            item["tmp_path"].write_bytes(item["payload"])
        for item in prepared:  # 全部 staging 落盘成功后才进入 rename 段
            item["tmp_path"].replace(item["target"])
            replaced.append(item["target"])
    except Exception:
        # 原子性回退（Eng §5.2:1「写入失败不登记文件」）：INSERT 段未达（无行），
        # 已 rename 目标与 staging 残留全清（无文件）；异常原样上抛由调用方回滚。
        for path in replaced:
            path.unlink(missing_ok=True)
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    shutil.rmtree(staging_dir, ignore_errors=True)  # 成功：批目录已清空即移除

    for item in prepared:
        db.add(
            TaskFile(
                id=item["id"],
                task_id=task.id,
                owner_id=task.owner_id,
                direction="input",
                file_name=item["file_name"],
                storage_key=f"tasks/{task.id}/{item['id']}",
                sha256=item["sha256"],
                size_bytes=item["size_bytes"],
                state="staged",
            )
        )
    await db.flush()
    return {
        "files": [
            {
                "id": str(item["id"]),
                "file_name": item["file_name"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "state": "staged",
            }
            for item in prepared
        ]
    }


async def delete_file(db: AsyncSession, *, owner_id: str, task_id: str, file_id: str) -> dict:
    """删除 staged 输入文件（D7a：uploading 限定；无字节账回退分支）。

    task 锁与 uploading 仲裁 → 行必须为存活 staged 输入（缺失/已墓碑/非本面 →
    FILE_NOT_FOUND 404）→ 条件 UPDATE 翻墓碑（state='deleted'）→ flush 后物理
    unlink（missing_ok；异常上抛由调用方回滚——墓碑随事务撤销）。

    返回 ``{"file": {"id", "state": "deleted"}}``。
    """
    task = await _lock_uploading_task(db, task_id)
    _parse_id(owner_id, "owner_id")
    fid = _parse_id(file_id, "file_id")
    row = (
        await db.execute(
            select(TaskFile).where(
                TaskFile.id == fid,
                TaskFile.task_id == task.id,
                TaskFile.direction == "input",
                TaskFile.state == "staged",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _file_not_found()
    tomb = await db.execute(
        update(TaskFile)
        .where(TaskFile.id == row.id, TaskFile.state == "staged")
        .values(state="deleted")
        .execution_options(synchronize_session=False)
    )
    if tomb.rowcount == 0:  # 任务行锁在握，理论不可达——条件仲裁双保险
        raise _file_not_found()
    await db.flush()
    path = _input_path_from_key(_settings_storage(), row.storage_key)
    if path is None:  # storage_key 形态异常（服务端数据缺陷）：按文件缺失处置
        raise _file_not_found()
    path.unlink(missing_ok=True)
    return {"file": {"id": str(row.id), "state": "deleted"}}


async def list_files(
    db: AsyncSession, *, owner_id: str, task_id: str, direction: str
) -> list[dict]:
    """输入/产物文件列表（含 sha256/size/state；Sup §4:103）。

    direction ∈ {input, output}（词表外 400）；deleted 墓碑不可见；任务缺失/
    他人/已删除统一 TASK_NOT_FOUND 404（Sup §7）。created_at ASC + id ASC 稳定序。
    """
    tid = _parse_id(task_id, "task_id")
    _parse_id(owner_id, "owner_id")
    if direction not in ("input", "output"):
        raise _validation_error("direction 必须为 input 或 output")
    exists = (
        await db.execute(select(Task.id).where(Task.id == tid, Task.status != "deleted"))
    ).scalar_one_or_none()
    if exists is None:
        raise _task_not_found()
    rows = (
        (
            await db.execute(
                select(TaskFile)
                .where(
                    TaskFile.task_id == tid,
                    TaskFile.direction == direction,
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
        }
        for r in rows
    ]


async def read_input_bytes(
    storage: TaskStorage, db: AsyncSession, *, owner_id: str, task_id: str, file_name: str
) -> bytes:
    """读取输入文件字节（T7 /internal/tools 回调消费；D16 owner 会话内调用）。

    file_name 只作行查询键、永不参与路径拼接（防穿越）；物理路径只认行内
    storage_key 严格解析派生。行查询钉 input 面（task_files 无 (task_id,
    file_name) 唯一约束——跨方向同名合法，committed 输入与 T7 产物行并存时
    只认 input 行，防 MultipleResultsFound 与产物路径错面派生）；存活口径
    （state != 'deleted'，墓碑即 FILE_NOT_FOUND，与 list_input_meta 对齐）。
    行不可见（缺失/他人/墓碑/键形态异常）或物理文件缺失（OSError）→
    FILE_NOT_FOUND 404。
    """
    tid = _parse_id(task_id, "task_id")
    uid = _parse_id(owner_id, "owner_id")
    if not isinstance(file_name, str) or not file_name:
        raise _file_not_found()
    row = (
        await db.execute(
            select(TaskFile).where(
                TaskFile.task_id == tid,
                TaskFile.owner_id == uid,
                TaskFile.direction == "input",
                TaskFile.file_name == file_name,
                TaskFile.state != "deleted",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _file_not_found()
    path = _input_path_from_key(storage, row.storage_key)
    if path is None:
        raise _file_not_found()
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _file_not_found() from exc


async def list_input_meta(db: AsyncSession, *, task_id: str) -> list[dict]:
    """输入文件元数据（T7 回调消费：任务快照 /task-files 面；调用方已设 GUC）。

    仅存活输入（direction='input'、墓碑不可见）；任务不可见时返回空列表（内部
    面——回调方已持任务令牌校验，不做 404 装配）。
    """
    tid = _parse_id(task_id, "task_id")
    rows = (
        await db.execute(
            select(TaskFile.id, TaskFile.file_name, TaskFile.sha256, TaskFile.size_bytes)
            .where(
                TaskFile.task_id == tid,
                TaskFile.direction == "input",
                TaskFile.state != "deleted",
            )
            .order_by(TaskFile.created_at.asc(), TaskFile.id.asc())
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "file_name": r.file_name,
            "sha256": r.sha256,
            "size_bytes": int(r.size_bytes),
        }
        for r in rows
    ]
