import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Briefcase,
  ListChecks,
  SealCheck,
  UserCircle,
} from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { displayName } from "../auth/displayName.js";
import { requestV2 } from "../api/v2/client.js";
import { V2_TASKS } from "../api/v2/routes.js";
import { formatDateTime } from "../lib/datetime.js";
import { TASK_STATUS_LABELS, toDisplayTask } from "../lib/taskDisplay.js";
import PasswordChangeCard from "../components/PasswordChangeCard.jsx";
import MfaCard from "../components/MfaCard.jsx";
import SessionsCard from "../components/SessionsCard.jsx";
import DangerZone from "../components/DangerZone.jsx";

export default function ProfilePage() {
  const { v2User, isExpert } = useAuth();
  // 身份渲染源 = V2 权威会话（T14 会话归一：唯一会话域）
  const displayUser = v2User;
  const name = displayName(displayUser);
  // 角色渲染源 = displayUser 实际 role（T2 账本 minor 顺手修：V2 admin 曾显示 "user"）
  const displayRole = displayUser?.role;
  // 安全区块为 V2 会话语义（password-change / mfa/* 均走 cookie 会话）
  const hasV2Session = Boolean(v2User);
  const [tasks, setTasks] = useState(null);
  const [taskError, setTaskError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        // 仅本人任务（服务端按会话归属过滤；V2_TASKS 列表）
        if (v2User) {
          const result = await requestV2(`${V2_TASKS}?page=1&size=20`);
          if (!cancelled) {
            setTasks((result.data?.items ?? []).map(toDisplayTask));
          }
        } else if (!cancelled) {
          setTasks([]);
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
  }, [v2User]);

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
              {/* 受理成功由 DangerZone 自行导航 /account/deleting（冻结页
                  独立路由、不挂守卫——V2-only 用户注销后匿名仍可达） */}
              <DangerZone />
            </>
          ) : null}

          <div className="profile-card rise" style={{ "--rise-index": 6 }}>
            <h2 className="profile-card-title">
              <Briefcase size={16} aria-hidden="true" />
              专家身份
            </h2>
            {isExpert ? (
              <p className="profile-expert-desc">
                你已是专家用户，可以创建并发布自己的专家，为其装配 Skill。
              </p>
            ) : (
              // 专家身份由管理员授予（expert_author entitlement）——展示说明
              // 文案而非 CTA（自助申请端点已随 V1 面删除，T11 ③/T14 收敛）
              <p className="profile-expert-desc">
                V2
                账户的专家身份由平台管理员授予（expert_author 权限）。获得授权后即可创建自己的专家、装配
                Skill 并发布到专家中心；如需开通，请联系管理员。
              </p>
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
                          {/* 双轨归一辅行：expert/provider 标签 · 创建时间（双轨 created_at 同为 ISO 串） */}
                          <span className="context-tool-desc">
                            {[
                              task.expertLabel,
                              task.createdAt ? formatDateTime(task.createdAt) : "",
                            ]
                              .filter(Boolean)
                              .join(" · ")}
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
