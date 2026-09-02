import { useState } from "react";
import {
  Briefcase,
  CheckCircle,
  ListChecks,
  SealCheck,
  UserCircle,
} from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";

function formatDateTime(isoString) {
  if (!isoString) {
    return "";
  }
  // 后端存 UTC 但序列化为无时区的 naive ISO 串；补 Z 避免被当作本地时间
  const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(isoString) ? isoString : `${isoString}Z`;
  return new Date(normalized).toLocaleString("zh-CN", { hour12: false });
}

export default function ProfilePage() {
  const { user, isExpert, applyExpert } = useAuth();
  const [isApplying, setIsApplying] = useState(false);
  const [applyError, setApplyError] = useState("");
  const [justApplied, setJustApplied] = useState(false);

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
            <div className="empty-state">
              <strong>还没有任务</strong>
              到专家中心召唤一位专家，开启你的第一个任务对话。
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
