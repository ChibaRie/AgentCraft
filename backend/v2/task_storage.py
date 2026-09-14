"""V2 任务物理存储布局（Phase 6 T2，D10 存储根隔离）。

物理根 ``<HOST_DATA_ROOT>/task-storage/``（``Settings.V2_TASK.storage_root`` 可覆盖）::

    staging/<batch>/                     上传暂存（commit 后移入 tasks/）
    tasks/<task_id>/inputs/<file_id>     已提交输入
    tasks/<task_id>/outputs/<file_id>    容器回调写文件
    artifacts/<task_id>/<file_id>        产物登记副本
    extensions/task-<task_id>.ts         任务扩展脚本（与 V1 extensions 根异名空间）

storage_key 保持逻辑形态 ``tasks/<uuid>/<uuid>``（task_files.storage_key，DB 契约），
物理路径由本模块派生。目录惰性创建：路径方法内 ``mkdir(parents=True, exist_ok=True)``，
构造与 ``v2_runtime_from_settings`` 均无启动 I/O（纯函数）。V1 的 workdir/任务卷
概念不进入本布局（双轨纪律：V1 task-files 根与清扫 glob 不受影响）。
"""

import shutil
from pathlib import Path


class TaskStorage:
    """任务域物理路径派生器。所有方法仅在首次触达时创建父目录，幂等且无预建。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def staging_dir(self, batch: str) -> Path:
        """上传暂存目录（批粒度，commit 后由服务层移入 tasks/）。"""
        path = self._root / "staging" / batch
        path.mkdir(parents=True, exist_ok=True)
        return path

    def input_path(self, task_id: str, file_id: str) -> Path:
        """已提交输入：tasks/<id>/inputs/<fid>。"""
        return self._task_file(task_id, "inputs", file_id)

    def output_path(self, task_id: str, file_id: str) -> Path:
        """容器回调写文件：tasks/<id>/outputs/<fid>。"""
        return self._task_file(task_id, "outputs", file_id)

    def artifact_path(self, task_id: str, file_id: str) -> Path:
        """产物登记副本：artifacts/<task_id>/<fid>。"""
        path = self._root / "artifacts" / task_id / file_id
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def extension_path(self, task_id: str) -> Path:
        """任务扩展脚本：extensions/task-<task_id>.ts（V2 独立子目录）。"""
        path = self._root / "extensions" / f"task-{task_id}.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def delete_task_storage(self, task_id: str) -> None:
        """删除任务物理树 tasks/<id>/ 与 artifacts/<id>/（幂等：不存在即静默）。

        extensions/task-<task_id>.ts 不在范围——扩展脚本由任务扩展管理路径负责。
        """
        for sub in ("tasks", "artifacts"):
            shutil.rmtree(self._root / sub / task_id, ignore_errors=True)

    def _task_file(self, task_id: str, sub: str, file_id: str) -> Path:
        path = self._root / "tasks" / task_id / sub / file_id
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
