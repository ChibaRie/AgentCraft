/**
 * 双轨任务行展示归一（Phase 8 T11，cutover 前并存；T14 收敛后仅余 V2 形状）。
 * HomePage / ProfilePage 的「最近任务/我的任务」共用。
 */

/**
 * 任务状态词表：V1（created/running/completed/failed）∪ V2（backend/v2/models/
 * tasking.py TASK_STATUSES；deleted 列表不可见，防御兜底）。cutover 后 V1 四枚
 * 随 V1 面删除（T14）。
 */
export const TASK_STATUS_LABELS = {
  created: "待开始",
  uploading: "上传中",
  queued: "排队中",
  running: "进行中",
  ready: "待继续",
  completed: "已结束",
  failed: "异常",
  aborted: "已中止",
  deleted: "已删除",
};

/**
 * 行形状归一：
 * - V1 行：{title, expert_name_snapshot, created_at}（无 expert/provider 键）；
 * - V2 行：Sup §10.3 任务视图 {expert:{name}|null, provider:{display_name}|null,
 *   created_at}，无 title——以 expert.name 作展示主行、provider.display_name
 *   作辅行。
 */
export function toDisplayTask(task) {
  return {
    id: task.id,
    status: task.status,
    title: task.title ?? task.expert?.name ?? null,
    expertLabel: task.expert_name_snapshot ?? task.provider?.display_name ?? "",
    createdAt: task.created_at ?? null,
  };
}
