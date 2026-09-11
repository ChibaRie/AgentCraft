import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Briefcase,
  CheckCircle,
  ListChecks,
  SealCheck,
  ShieldWarning,
  UserCircle,
} from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { displayName } from "../auth/displayName.js";
import { request } from "../api/client.js";
import { formatDateTime } from "../lib/datetime.js";
import PasswordChangeCard from "../components/PasswordChangeCard.jsx";
import MfaCard from "../components/MfaCard.jsx";
import SessionsCard from "../components/SessionsCard.jsx";
import DangerZone from "../components/DangerZone.jsx";

const TASK_STATUS_LABELS = {
  created: "待开始",
  running: "进行中",
  completed: "已结束",
  failed: "异常",
};

export default function ProfilePage() {
  const { user, v2User, isExpert, applyExpert } = useAuth();
  // 双轨身份源（E12）：V1 会话优先，V2-only 用户回退
  const displayUser = user ?? v2User;
  const name = displayName(displayUser);
  // 角色渲染源 = displayUser 实际 role（T2 账本 minor 顺手修：V2 admin 曾显示 "user"）
  const displayRole = displayUser?.role;
  // 安全区块为 V2 会话语义（password-change / mfa/* 均走 cookie 会话）：
  // 仅在存在 V2 会话时渲染，V1-only 用户不暴露必 401 的操作面
  const hasV2Session = Boolean(v2User);
  const [isApplying, setIsApplying] = useState(false);
  const [applyError, setApplyError] = useState("");
  const [justApplied, setJustApplied] = useState(false);
  const [tasks, setTasks] = useState(null);
  const [taskError, setTaskError] = useState("");
  // 注销受理后的剩余天数（null = 未受理）：非空时接管整页渲染（FE-T7）
  const [deletionDaysRemaining, setDeletionDaysRemaining] = useState(null);

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

  // 注销受理后的全页冻结展示态（FE-T7）：接管整页渲染，冻结后续 V2 请求
  // （V2 本地态已由 DangerZone 清理：csrf 清空 + v2User 置空；V1 工作区会话
  // 是独立域不受影响——此处须先于一切 displayUser 消费，防 V2-only 用户
  // v2User=null 后解引用空对象）
  if (deletionDaysRemaining !== null) {
    return (
      <main className="app-main">
        <section className="v2-deleting-page rise" role="status">
          <ShieldWarning size={28} weight="fill" aria-hidden="true" />
          <h1 className="v2-deleting-title">账户注销中</h1>
          <p className="v2-deleting-lead">
            账户注销申请已受理，<strong>{deletionDaysRemaining} 天后生效</strong>。
          </p>
          <p className="v2-deleting-note">
            恢复链接已发送至邮箱，宽限期内可凭邮件中的恢复链接撤销注销、
            恢复账户的正常使用。
          </p>
          <p className="v2-deleting-domain">
            本次注销仅针对新账户体系（V2 账户域）；旧版工作区账户的登录不受影响。
          </p>
        </section>
      </main>
    );
  }

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
        <section aria-label="账号与安全">
          <div className="profile-card rise" style={{ "--rise-index": 1 }}>
            <h2 className="profile-card-title">
              <UserCircle size={16} aria-hidden="true" />
              账号信息
            </h2>
            <div className="profile-identity">
              <span className="profile-avatar" aria-hidden="true">
                {name.slice(0, 1).toUpperCase()}
              </span>
              <div>
                <div className="profile-name-row">
                  <span className="profile-name">{name}</span>
                  {displayRole === "expert" ? (
                    <span className="role-badge is-expert">
                      <SealCheck size={12} weight="fill" aria-hidden="true" />
                      专家
                    </span>
                  ) : displayRole === "admin" ? (
                    <span className="role-badge">管理员</span>
                  ) : (
                    <span className="role-badge">普通用户</span>
                  )}
                </div>
                <div className="profile-email">{displayUser.email}</div>
              </div>
            </div>
            <div className="profile-meta">
              <div className="profile-meta-row">
                <span className="profile-meta-key">用户 ID</span>
                <span className="profile-meta-value">{displayUser.id}</span>
              </div>
              <div className="profile-meta-row">
                <span className="profile-meta-key">角色</span>
                <span className="profile-meta-value">{displayRole ?? "user"}</span>
              </div>
              <div className="profile-meta-row">
                <span className="profile-meta-key">注册时间</span>
                {/* V2 user 形状无 created_at——显示「—」（形状缺陷兜底） */}
                <span className="profile-meta-value">
                  {displayUser.created_at ? formatDateTime(displayUser.created_at) : "—"}
                </span>
              </div>
            </div>
          </div>

          {hasV2Session ? (
            <>
              <SessionsCard />
              <PasswordChangeCard />
              <MfaCard />
              <DangerZone onDeletionRequested={setDeletionDaysRemaining} />
            </>
          ) : null}

          <div className="profile-card rise" style={{ "--rise-index": 6 }}>
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
          <div className="profile-card rise" style={{ "--rise-index": 7 }}>
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
