"""TaskStorage 路径派生与惰性目录/删除幂等单元测试（Phase 6 T2，tmp_path）。

D10 布局：staging/<batch>/、tasks/<id>/inputs|outputs/<fid>、
artifacts/<id>/<fid>、extensions/task-<id>.ts。构造无 I/O（runtime 纯函数纪律）。
"""

from pathlib import Path

from backend.v2.task_storage import TaskStorage

TASK_ID = "0191b6f6-8a2e-7111-8111-000000000001"
FILE_ID = "0191b6f6-8a2e-7111-8111-000000000002"
OTHER_ID = "0191b6f6-8a2e-7111-8111-000000000003"


def test_constructor_does_no_io(tmp_path: Path) -> None:
    root = tmp_path / "task-storage"
    TaskStorage(root)
    assert not root.exists()  # 纯函数构造：目录惰性创建（v2_runtime_from_settings 无启动 I/O）


def test_path_derivations(tmp_path: Path) -> None:
    storage = TaskStorage(tmp_path)
    assert storage.input_path(TASK_ID, FILE_ID) == (
        tmp_path / "tasks" / TASK_ID / "inputs" / FILE_ID
    )
    assert storage.output_path(TASK_ID, FILE_ID) == (
        tmp_path / "tasks" / TASK_ID / "outputs" / FILE_ID
    )
    assert storage.artifact_path(TASK_ID, FILE_ID) == (tmp_path / "artifacts" / TASK_ID / FILE_ID)
    assert storage.extension_path(TASK_ID) == tmp_path / "extensions" / f"task-{TASK_ID}.ts"
    assert storage.staging_dir("batch-1") == tmp_path / "staging" / "batch-1"


def test_lazy_mkdir_on_path_methods(tmp_path: Path) -> None:
    """路径方法惰性建父目录（exist_ok 幂等）：目录在、文件不在；root 一并惰性建立。"""
    storage = TaskStorage(tmp_path)
    p = storage.input_path(TASK_ID, FILE_ID)
    assert p.parent.is_dir() and not p.exists()
    assert storage.output_path(TASK_ID, FILE_ID).parent.is_dir()
    assert storage.artifact_path(TASK_ID, FILE_ID).parent.is_dir()
    assert storage.extension_path(TASK_ID).parent.is_dir()
    assert storage.staging_dir("batch-9").is_dir()
    # 幂等：重复派生不抛
    assert storage.input_path(TASK_ID, FILE_ID) == p


def test_delete_task_storage_removes_only_own_trees(tmp_path: Path) -> None:
    storage = TaskStorage(tmp_path)
    for path in (
        storage.input_path(TASK_ID, FILE_ID),
        storage.output_path(TASK_ID, FILE_ID),
        storage.artifact_path(TASK_ID, FILE_ID),
    ):
        path.write_bytes(b"x")
    keep_artifact = storage.artifact_path(OTHER_ID, FILE_ID)
    keep_artifact.write_bytes(b"keep")
    keep_extension = storage.extension_path(OTHER_ID)
    keep_extension.write_bytes(b"keep")

    storage.delete_task_storage(TASK_ID)

    assert not (tmp_path / "tasks" / TASK_ID).exists()
    assert not (tmp_path / "artifacts" / TASK_ID).exists()
    assert keep_artifact.exists()  # 他人任务产物不受影响
    assert keep_extension.exists()  # extensions/ 不在删除范围（扩展管理路径负责）


def test_delete_task_storage_is_idempotent(tmp_path: Path) -> None:
    storage = TaskStorage(tmp_path)
    storage.delete_task_storage(TASK_ID)  # 目录从未存在：静默
    storage.input_path(TASK_ID, FILE_ID).write_bytes(b"x")
    storage.delete_task_storage(TASK_ID)
    storage.delete_task_storage(TASK_ID)  # 重复删除：仍静默
    assert not (tmp_path / "tasks" / TASK_ID).exists()
    assert not (tmp_path / "artifacts" / TASK_ID).exists()
