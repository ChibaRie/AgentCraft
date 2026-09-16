import { V2ApiError, requestV2 } from "../api/v2/client.js";
import { V2_TASKS } from "../api/v2/routes.js";
import { messagesPath } from "./taskChatReducer.js";

// ---------------------------------------------------------------------------
// 数据装载（五端点并行）
// ---------------------------------------------------------------------------

/** 初始装配：快照 + messages?after=0 + files(input/output) + artifacts（Sup §1.2/§4）。 */
export async function loadTaskBundle(taskId) {
  const base = `${V2_TASKS}/${encodeURIComponent(taskId)}`;
  const [taskRes, msgRes, inputRes, outputRes, artifactRes] = await Promise.all([
    requestV2(base),
    requestV2(messagesPath(taskId, 0)),
    requestV2(`${base}/files?direction=input`),
    requestV2(`${base}/files?direction=output`),
    requestV2(`${base}/artifacts`),
  ]);
  const task = taskRes.data?.task ?? null;
  if (!task) {
    throw new V2ApiError("INVALID_RESPONSE", "任务快照缺少 task 字段", taskRes.status);
  }
  return {
    task,
    messages: Array.isArray(msgRes.data) ? msgRes.data : [],
    inputFiles: Array.isArray(inputRes.data) ? inputRes.data : [],
    artifacts: Array.isArray(artifactRes.data) ? artifactRes.data : [],
  };
}
