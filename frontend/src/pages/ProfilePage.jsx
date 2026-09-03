import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Briefcase,
  CheckCircle,
  ListChecks,
  SealCheck,
  UserCircle,
} from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { request } from "../api/client.js";
import { formatDateTime } from "../lib/datetime.js";

const TASK_STATUS_LABELS = {
  created: "待开始",
  running: "进行中",
  completed: "已结束",
  failed: "异常",
};

export default function ProfilePage() {
  const { user, isExpert, applyExpert } = useAuth();
  const [isApplying, setIsApplying] = useState(false);
  const [applyError, setApplyError] = useState("");
  const [justApplied, setJustApplied] = useState(false);
  const [tasks, setTasks] = useState(null);
  const [taskError, setTaskError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        // §8.2 P05 TaskList：仅本人任务（服务端按 token 归属过滤）
        const payload = await request("/api/tasks?page=1&size=20");
        if (!cancelled) {
          setTasks(payload.data);
        }
      } catch (error) {
        if (!cancelled) {
          setTaskError(error.message || "任务列表加载失败");
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleApplyExpert() {
    setIsApplying(true);
    setApplyError("");
    try {
      await applyExpert();
      setJustApplied(true);
    } catch (error) {
      setApplyError(error.message || "申请失败，请稍后重试");
    } finally {
      setIsApplying(false);
    }
  }

  return (
    <main className="app-main">
      <header className="page-header rise">
        <h1 className="page-title">个人中心</h1>
        <p className="page-sub">管理你的账号身份，随时开启专家之路。</p>
      </header>

      <div className="profile-grid">
        <section aria-label="账号信息">
          <div className="profile-card rise" style={{ "--rise-index": 1 }}>
            <h2 className="profile-card-title">
              <UserCircle size={16} aria-hidden="true" />
              账号信息
            </h2>
            <div className="profile-identity">
              <span className="profile-avatar" aria-hidden="true">
                {user.username.slice(0, 1).toUpperCase()}
              </span>
              <div>
                <div className="profile-name-row">
                  <span className="profile-name">{user.username}</span>
                  {isExpert ? (
                    <span className="role-badge is-expert">
                      <SealCheck size={12} weight="fill" aria-hidden="true" />
                      专家
                    </span>
                  ) : (
                    <span className="role-badge">普通用户</span>
                  )}
                </div>
                <div className="profile-email">{user.email}</div>
              </div>
            </div>
            <div className="profile-meta">
              <div className="profile-meta-row">
                <span className="profile-meta-key">用户 ID</span>
                <span className="profile-meta-value">{user.id}</span>
              </div>
              <div className="profile-meta-row">
                <span className="profile-meta-key">角色</span>
                <span className="profile-meta-value">{isExpert ? "expert" : "user"}</span>
              </div>
              <div className="profile-meta-row">
                <span className="profile-meta-key">注册时间</span>
                <span className="profile-meta-value">{formatDateTime(user.created_at)}</span>
              </div>
            </div>
          </div>

          <div className="profile-card rise" style={{ "--rise-index": 2 }}>
            <h2 className="profile-card-title">
              <Briefcase size={16} aria-hidden="true" />
              专家身份
            </h2>
            {isExpert ? (
              <p className="profile-expert-desc">
                {justApplied && (
                  <>
                    <CheckCircle size={14} weight="fill" aria-hidden="true" /> 申请成功。
                  </>
                )}
                {justApplied ? " " : ""}
                你已是专家用户，可以创建并发布自己的专家，为其装配 Skill 与 MCP 工具。
              </p>
            ) : (
              <div className="profile-expert-cta">
                <p className="profile-expert-desc">
                  成为专家用户后，你可以创建自己的专家：定义人设与方法论、装配 Skill 与
                  MCP 工具、发布到专家中心供他人召唤。申请即时生效。
                </p>
                <div className="form-alert" role="alert" hidden={!applyError}>
                  {applyError}
                </div>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleApplyExpert}
                  disabled={isApplying}
                >
                  {isApplying ? "申请中…" : "申请专家身份"}
                </button>
              </div>
            )}
          </div>
        </section>

        <section aria-label="我的任务">
          <div className="profile-card rise" style={{ "--rise-index": 3 }}>
            <h2 className="profile-card-title">
              <ListChecks size={16} aria-hidden="true" />
              我的任务
            </h2>
            {taskError && (
              <div className="form-alert" role="alert" style={{ marginBottom: 12 }}>
                {taskError}
              </div>
            )}
            {tasks === null && !taskError ? (
              <p className="profile-expert-desc">加载中…</p>
            ) : tasks && tasks.length === 0 ? (
              <div className="empty-state">
                <strong>还没有任务</strong>
                到专家中心召唤一位专家，开启你的第一个任务对话。
              </div>
            ) : (
              tasks && (
                <ul className="binding-list">
                  {tasks.map((task) => (
                    <li className="binding-row" key={task.id}>
                      <div className="binding-row-main">
                        <Link to={`/tasks/${task.id}`} className="binding-row-link">
                          <strong>{task.title || `任务 #${task.id}`}</strong>
                          <span className="context-tool-desc">
                            {task.expert_name_snapshot} · {formatDateTime(task.created_at)}
                          </span>
                        </Link>
                      </div>
                      <span className={`status-chip is-${task.status}`}>
                        {TASK_STATUS_LABELS[task.status] || task.status}
                      </span>
                    </li>
                  ))}
                </ul>
              )
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
