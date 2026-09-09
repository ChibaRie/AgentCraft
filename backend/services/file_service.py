"""任务文件存储服务（Engineering Spec §6.6 上传规则）。

职责：文件名校验、单文件/数量/任务配额、UUID 存储名、流式 SHA-256、
no-follow 落盘与失败补偿（暂存 → 校验 → 移动 → 由调用方提交 DB）。
文件内容不进 SQLite；事实源是 TaskFile 行 + 磁盘文件。
"""

import hashlib
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select

from backend.services.user_service import UserSystemError

logger = logging.getLogger("agentcraft")

_CHUNK_SIZE = 256 * 1024
_STAGING_DIR_NAME = ".staging"
_STAGING_MAX_AGE_SECONDS = 3600
_MIME_PATTERN = re.compile(r"^[\w.+-]+/[\w.+-]+$")
_MIME_MAX_LENGTH = 100


class FilenameInvalidError(UserSystemError):
    status_code = 400
    code = "FILENAME_INVALID"


class FileTooLargeError(UserSystemError):
    status_code = 413
    code = "FILE_TOO_LARGE"


class FileCountExceededError(UserSystemError):
    status_code = 413
    code = "FILE_COUNT_EXCEEDED"


class FileQuotaExceededError(UserSystemError):
    status_code = 413
    code = "FILE_QUOTA_EXCEEDED"


class FileStorageError(UserSystemError):
    status_code = 507
    code = "FILE_STORAGE_ERROR"


def _is_control_char(text: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in text)


def _sanitize_mime(content_type: str | None) -> str | None:
    """客户端声明的 MIME 仅用于展示（§6.6）；非法/超长一律丢弃。"""
    if not content_type:
        return None
    value = content_type.strip().split(";")[0].strip().lower()
    if not value or len(value) > _MIME_MAX_LENGTH or not _MIME_PATTERN.match(value):
        return None
    return value


def _is_within_root(root: Path, path: Path) -> bool:
    """resolve 包含性校验：junction/symlink 逃逸一律判否（is_symlink 在
    Windows 上不识别 NTFS junction，不能单独依赖）。"""
    resolved_root = Path(root).resolve()
    resolved = Path(path).resolve()
    return resolved_root == resolved or resolved_root in resolved.parents


class FileService:
    def __init__(
        self,
        root: Path,
        max_single_bytes: int = 20 * 1024 * 1024,
        max_files_per_request: int = 10,
        max_task_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self.root = Path(root)
        self.max_single_bytes = max_single_bytes
        self.max_files_per_request = max_files_per_request
        self.max_task_bytes = max_task_bytes

    # -- 文件名规则 --------------------------------------------------------

    def validate_original_name(self, name: str | None) -> str:
        """仅允许独立文件的 basename；拒绝空名、路径分隔符、控制字符与超长。"""
        if not name:
            raise FilenameInvalidError("文件名不能为空")
        if "/" in name or "\\" in name:
            raise FilenameInvalidError("文件名不能包含路径分隔符")
        if name in (".", ".."):
            raise FilenameInvalidError("文件名非法")
        if _is_control_char(name):
            raise FilenameInvalidError("文件名包含控制字符")
        if not 1 <= len(name) <= 255:
            raise FilenameInvalidError("文件名长度必须在 1-255 字符之间")
        return name

    # -- 暂存与落盘 --------------------------------------------------------

    async def stage_batch(self, task_id: int, uploads: list[UploadFile]) -> list[dict]:
        """整批暂存到 .staging/<批次>/；任一文件失败则清空本批并抛错。"""
        if len(uploads) > self.max_files_per_request:
            raise FileCountExceededError(
                f"单次最多上传 {self.max_files_per_request} 个文件"
            )
        batch_dir = self.root / _STAGING_DIR_NAME / uuid.uuid4().hex
        try:
            self._ensure_dir_writable(batch_dir)
        except FileStorageError:
            raise
        staged: list[dict] = []
        try:
            for upload in uploads:
                original_name = self.validate_original_name(upload.filename)
                stored_name = uuid.uuid4().hex
                target = batch_dir / stored_name
                size_bytes, sha256 = await self._receive(upload, target)
                staged.append(
                    {
                        "task_id": task_id,
                        "original_name": original_name,
                        "stored_name": stored_name,
                        "relative_path": f"task-{task_id}/{stored_name}",
                        "size_bytes": size_bytes,
                        "mime_type": _sanitize_mime(upload.content_type),
                        "sha256": sha256,
                        "staging_path": str(target),
                        "final_path": None,
                    }
                )
            # 观测（红线 §4.7）：只记 task_id/文件数/字节数，文件正文不落日志
            logger.info(
                "Task %s: 已暂存 %d 个文件（%d 字节）",
                task_id,
                len(staged),
                sum(item["size_bytes"] for item in staged),
            )
            return staged
        except Exception as exc:
            self._remove_tree(batch_dir)
            logger.warning(
                "Task %s: 暂存失败已清空本批（已收 %d 个文件，错误 %s）",
                task_id,
                len(staged),
                type(exc).__name__,
            )
            raise

    def place_batch(self, task_id: int, staged: list[dict]) -> None:
        """整批移动到最终目录 task-{id}/；失败时清空本批暂存/已移动文件。"""
        final_dir = self.root / f"task-{task_id}"
        if not _is_within_root(self.root, final_dir):
            raise FileStorageError("任务文件目录非法（路径逃逸）")
        if final_dir.is_symlink():
            raise FileStorageError("任务文件目录非法（符号链接）")
        moved: list[Path] = []
        try:
            self._ensure_dir_writable(final_dir)
            for item in staged:
                source = Path(item["staging_path"])
                target = self.root / item["relative_path"]
                source.replace(target)
                item["final_path"] = str(target)
                moved.append(target)
            logger.info(
                "Task %s: 已落盘 %d 个文件（%d 字节）",
                task_id,
                len(moved),
                sum(item["size_bytes"] for item in staged),
            )
        except Exception as exc:
            for target in moved:
                target.unlink(missing_ok=True)
            for item in staged:
                Path(item["staging_path"]).unlink(missing_ok=True)
            logger.warning(
                "Task %s: 落盘失败已回滚（%d 个文件，错误 %s）",
                task_id,
                len(staged),
                type(exc).__name__,
            )
            raise

    def discard(self, staged: list[dict]) -> None:
        """失败补偿：删除本批已暂存与已移动的文件（尽力而为），并清掉空任务目录。"""
        task_dirs: set[Path] = set()
        for item in staged:
            for key in ("final_path", "staging_path"):
                path = item.get(key)
                if path:
                    Path(path).unlink(missing_ok=True)
            task_dirs.add(self.root / f"task-{item['task_id']}")
        for directory in task_dirs:
            try:
                directory.rmdir()  # 仅在为空时成功
            except OSError:
                pass

    def total_task_bytes(self, existing_sum: int, staged: list[dict]) -> int:
        return existing_sum + sum(item["size_bytes"] for item in staged)

    def assert_task_quota(self, new_total: int) -> None:
        if new_total > self.max_task_bytes:
            raise FileQuotaExceededError(
                f"任务累计文件大小超过 {self.max_task_bytes} 字节限制"
            )

    # -- 内部工具 ----------------------------------------------------------

    async def _receive(self, upload: UploadFile, target: Path) -> tuple[int, str]:
        """流式写盘并计算 SHA-256；超过单文件限额立即中断。"""
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with target.open("wb") as sink:
                while chunk := await upload.read(_CHUNK_SIZE):
                    size_bytes += len(chunk)
                    if size_bytes > self.max_single_bytes:
                        raise FileTooLargeError(
                            f"单文件大小超过 {self.max_single_bytes} 字节限制"
                        )
                    digest.update(chunk)
                    sink.write(chunk)
        except OSError as exc:
            raise FileStorageError("文件写入失败") from exc
        return size_bytes, digest.hexdigest()

    def _ensure_dir_writable(self, directory: Path) -> None:
        """以 no-follow 语义创建目录：任一级符号链接或 resolve 后越出根目录则拒绝。"""
        for ancestor in directory.parents:
            if ancestor.is_symlink():
                raise FileStorageError("存储目录非法（符号链接）")
        if directory.is_symlink():
            raise FileStorageError("存储目录非法（符号链接）")
        if not _is_within_root(self.root, directory):
            raise FileStorageError("存储目录非法（路径逃逸）")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FileStorageError("存储目录不可用") from exc

    def _remove_tree(self, directory: Path) -> None:
        shutil.rmtree(directory, ignore_errors=True)


def _sweep_staging(root: Path, removed: dict[str, int]) -> None:
    """清理超过保留期的暂存批次（崩溃时未能走完补偿流程的批次）。"""
    staging_dir = root / _STAGING_DIR_NAME
    if not staging_dir.is_dir():
        return
    deadline = time.time() - _STAGING_MAX_AGE_SECONDS
    for batch in staging_dir.iterdir():
        try:
            if batch.stat().st_mtime < deadline:
                shutil.rmtree(batch, ignore_errors=True)
                removed["staging_batches"] += 1
        except OSError:
            continue


def _sweep_orphans(root: Path, known_paths: set[str], removed: dict[str, int]) -> None:
    """以 TaskFile 为事实源删除无元数据孤儿文件；清空的任务目录一并移除。

    junction/symlink 指向根外的任务目录整体跳过（绝不跟随删除）。
    """
    for task_dir in sorted(root.glob("task-*")):
        if not task_dir.is_dir() or task_dir.is_symlink():
            continue
        if not _is_within_root(root, task_dir):
            continue
        for stored in task_dir.iterdir():
            if not stored.is_file():
                continue
            if f"{task_dir.name}/{stored.name}" not in known_paths:
                try:
                    stored.unlink()
                    removed["orphan_files"] += 1
                except OSError:
                    continue
        try:
            task_dir.rmdir()  # 仅在为空时成功
        except OSError:
            pass


async def sweep_stale_storage(session_factory, root: Path) -> dict[str, int]:
    """启动巡检（§6.6）：清理超时暂存批次与无 TaskFile 元数据的孤儿文件。

    进程崩溃无法形成跨 SQLite/文件系统的原子事务，以上传补偿语义的兜底。
    """
    removed = {"staging_batches": 0, "orphan_files": 0}
    root = Path(root)
    if not root.is_dir():
        return removed

    _sweep_staging(root, removed)

    from backend.models.task_file import TaskFile

    async with session_factory() as session:
        rows = await session.execute(select(TaskFile.relative_path))
    _sweep_orphans(root, set(rows.scalars()), removed)
    # 观测（红线 §4.7）：只记清理计数，不记任何文件路径/内容；
    # 失败补偿路径由 main.py lifespan 的 logger.exception 统一记录
    logger.info(
        "存储巡检完成：清理超时暂存批次 %d 个、孤儿文件 %d 个",
        removed["staging_batches"],
        removed["orphan_files"],
    )
    return removed
