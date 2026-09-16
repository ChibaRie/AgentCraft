import { useEffect, useState } from "react";
import { requestV2 } from "../api/v2/client.js";
import { V2_TASKS } from "../api/v2/routes.js";
import { formatDateTime } from "../lib/datetime.js";
import { formatBytes } from "../lib/format.js";

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

/** task_rounds.state 词表展示（Sup §8:169）。 */
const ROUND_STATE_LABELS = {
  pending: "待领取",
  running: "执行中",
  cancelling: "终止中",
  settled: "已完成",
  failed: "已失败",
  cancelled: "已取消",
};

/** task_files.state 词表展示（Sup §8:172）。 */
const FILE_STATE_LABELS = {
  staged: "已暂存",
  committed: "已冻结",
  registered: "已登记",
  deleted: "已删除",
};

function SectionNote({ children }) {
  return <p className="context-note">{children}</p>;
}

function EmptyHint({ children }) {
  return <p className="context-empty">{children}</p>;
}

/** expert 卡：快照 expert.name/avatar_url（§10.3——takedown/指针前移后为 null） */
function ExpertCard({ task }) {
  const name = task.expert?.name || "未知专家";
  const avatarUrl = task.expert?.avatar_url ?? null;
  const provider = task.provider;
  return (
    <div className="context-card">
      {avatarUrl ? (
        <img className="discover-avatar" src={avatarUrl} alt={name} width="40" height="40" />
      ) : (
        <span className="discover-avatar" aria-hidden="true">
          {name.slice(0, 1)}
        </span>
      )}
      <div>
        <div className="context-card-name">{name}</div>
        <div className="context-card-desc">
          {provider
            ? `${provider.display_name ?? ""}${provider.model ? ` · ${provider.model}` : ""}`
            : "Provider 快照不可见"}
        </div>
      </div>
    </div>
  );
}

/** quota 条：任务粒度用量视图（GET /tasks/{id}/quota，失败静默——权威在写事务） */
function QuotaBar({ taskId, inputFiles, artifacts }) {
  const [quota, setQuota] = useState(null);

  useEffect(() => {
    if (!taskId) {
      return undefined;
    }
    let cancelled = false;
    requestV2(`${V2_TASKS}/${encodeURIComponent(taskId)}/quota`)
      .then((result) => {
        if (!cancelled) {
          setQuota(result.data ?? null);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setQuota(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, inputFiles.length, artifacts.length]);

  if (!quota) {
    return null;
  }
  const usage = quota.usage ?? {};
  const limits = quota.limits ?? {};
  return (
    <SectionNote>
      用量：输入 {usage.inputs_count ?? 0}/{limits.max_files_per_task ?? "-"} 个 · 累计{" "}
      {usage.total_bytes ?? 0}/{limits.max_task_bytes ?? "-"} 字节 ·{" "}
      {quota.input_frozen ? "输入已冻结" : "输入未冻结"}
    </SectionNote>
  );
}

/** 输入 manifest 列表（GET files?direction=input 的行形状：file_name/sha256/size/state） */
function InputManifest({ inputFiles }) {
  if (inputFiles.length === 0) {
    return <EmptyHint>无输入文件</EmptyHint>;
  }
  return (
    <ul className="event-list">
      {inputFiles.map((file) => (
        <li className="event-item" key={file.id}>
          <div className="event-top">
            <span className="event-name">{file.file_name}</span>
            <span className="event-time">{formatBytes(file.size_bytes)}</span>
            <span className="event-state">{FILE_STATE_LABELS[file.state] ?? file.state}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

/** 产物列表：GET /artifacts/{file_id}/download 直链下载（attachment 头由服务端置） */
function ArtifactList({ taskId, artifacts }) {
  if (artifacts.length === 0) {
    return <EmptyHint>暂无产物</EmptyHint>;
  }
  return (
    <ul className="event-list">
      {artifacts.map((artifact) => (
        <li className="event-item" key={artifact.id}>
          <div className="event-top">
            <a
              className="event-name"
              href={`${BASE_URL}${V2_TASKS}/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifact.id)}/download`}
              download={artifact.file_name}
            >
              {artifact.file_name}
            </a>
            <span className="event-time">{formatBytes(artifact.size_bytes)}</span>
            <span className="event-state">{FILE_STATE_LABELS[artifact.state] ?? artifact.state}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

/** rounds 时间线（快照仅含 initial/active 两轮摘要：id/state/attempt——D14 红线） */
function RoundsTimeline({ task }) {
  const rounds = [task.initial_round, task.active_round].filter(Boolean);
  const seen = new Set();
  const unique = rounds.filter((round) => {
    if (seen.has(round.id)) {
      return false;
    }
    seen.add(round.id);
    return true;
  });
  if (unique.length === 0) {
    return <EmptyHint>暂无轮次记录</EmptyHint>;
  }
  return (
    <ul className="event-list">
      {unique.map((round) => (
        <li className="event-item" key={round.id}>
          <div className="event-top">
            <span className="event-name">{round.id}</span>
            <span className="event-state">{ROUND_STATE_LABELS[round.state] ?? round.state}</span>
            <span className="event-time">attempt {round.attempt}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

/**
 * 右侧上下文面板（Phase 8 T9 按 V2 数据源重写）：expert 卡 + 输入 manifest +
 * 产物下载直链 + quota 条 + rounds 时间线。数据全部经 props 注入（任务快照/
 * files/artifacts 由对话页五端点装配），quota 视图由面板自取（失败静默）。
 * V1 的 Skill 快照/调用记录 tab 随契约移除：任务视图不加 skills 键（Sup §10.3），
 * 工具调用以消息流内工具卡片呈现。
 */
export default function TaskContextPanel({ taskId, task, inputFiles = [], artifacts = [] }) {
  if (!task) {
    return null;
  }
  return (
    <aside className="context-panel rise" style={{ "--rise-index": 2 }} aria-label="任务上下文">
      <div className="context-body" role="tabpanel" aria-label="专家">
        <ExpertCard task={task} />
        <SectionNote>专家与 Provider 在任务创建时冻结为快照，此后修改不影响本任务。</SectionNote>
        <QuotaBar taskId={taskId} inputFiles={inputFiles} artifacts={artifacts} />
      </div>

      <div className="context-body" role="tabpanel" aria-label="文件">
        <p className="context-card-name">输入文件（已冻结）</p>
        <InputManifest inputFiles={inputFiles} />
        <p className="context-card-name">产物</p>
        <ArtifactList taskId={taskId} artifacts={artifacts} />
      </div>

      <div className="context-body" role="tabpanel" aria-label="轮次">
        <SectionNote>
          任务创建于 {formatDateTime(task.created_at)}；每轮对话由调度器租槽执行。
        </SectionNote>
        <RoundsTimeline task={task} />
      </div>
    </aside>
  );
}
