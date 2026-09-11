import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext.jsx";
import AuthBrandPanel from "../components/AuthBrandPanel.jsx";
import FormField from "../components/FormField.jsx";
import { useResendVerification } from "../hooks/useResendVerification.js";
import { useRetryAfter } from "../hooks/useRetryAfter.js";

/**
 * 邀请接受落地页（FE-T4）：
 * - 令牌驱动公开流（E11）：不挂 RequireGuest——存量 V1 用户点邀请链接是合法
 *   路径；端点公开、成功即种新 V2 会话。独立于应用壳（P01 全屏构图）。
 * - token 从 query 预填（只读展示）；email 预填可改，query 无参数时降级手输
 *   （outbox 白名单仅 {action_token, valid_hours}，email 是否进 URL 属部署期
 *   模板能力，前端不得依赖）。
 * - 提交 → AuthContext.acceptInvitation（accept 幂等键 + 存 csrf + silent 探测
 *   users/me 置位 v2User）→ 引导页；探测未确认（重放/竞态 401）→ 引导文案 +
 *   前往登录，不得渲染任何调用认证端点的按钮（resend 无会话必 401）。
 * - 409 INVITATION_INVALID → 失效态页（无入口按钮）；429 → Retry-After 倒计时。
 */

/** 引导页重发区（探测已确认会话时渲染；认证端点调用只在有会话分支出现） */
function OnboardingResend() {
  const { resend, cooldown, sending, sent, error } = useResendVerification();
  return (
    <div className="auth-form-footer">
      <button
        type="button"
        className="btn btn-primary btn-block"
        onClick={resend}
        disabled={sending || cooldown > 0}
      >
        {cooldown > 0 ? `重新发送（${cooldown}s）` : sending ? "发送中…" : "重发验证邮件"}
      </button>
      {sent ? (
        <p className="v2-retry-hint" role="status">
          验证邮件已重发，请查收
        </p>
      ) : null}
      {error ? (
        <p className="v2-retry-hint" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export default function InvitationAcceptPage() {
  const [searchParams] = useSearchParams();
  const { acceptInvitation } = useAuth();
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  // 态机：form（表单）→ onboarding（引导）| invalid（409 失效态）
  const [stage, setStage] = useState("form");
  const [sessionConfirmed, setSessionConfirmed] = useState(false);
  const [acceptedEmail, setAcceptedEmail] = useState("");
  const [fields, setFields] = useState({
    email: searchParams.get("email") || "",
    password: "",
    confirmPassword: "",
  });
  const [errors, setErrors] = useState({});
  const [alert, setAlert] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const invitationToken = searchParams.get("token") || "";

  function setField(name, value) {
    setFields((current) => ({ ...current, [name]: value }));
    setErrors((current) => ({ ...current, [name]: "" }));
    setAlert("");
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const email = fields.email.trim();
    const nextErrors = {
      email: email ? "" : "请输入邮箱",
      password: fields.password ? "" : "请设置密码",
      confirmPassword: fields.confirmPassword ? "" : "请再次输入密码",
    };
    if (!nextErrors.confirmPassword && fields.confirmPassword !== fields.password) {
      nextErrors.confirmPassword = "两次输入的密码不一致";
    }
    setErrors(nextErrors);
    if (nextErrors.email || nextErrors.password || nextErrors.confirmPassword) {
      return;
    }
    setSubmitting(true);
    setAlert("");
    try {
      const result = await acceptInvitation(email, fields.password, invitationToken);
      setAcceptedEmail(email);
      setSessionConfirmed(result.sessionConfirmed);
      setStage("onboarding");
    } catch (error) {
      if (error.code === "INVITATION_INVALID") {
        setStage("invalid");
        return;
      }
      if (error.status === 429) {
        startRetryAfter(error.retryAfter);
        setAlert(error.message || "请求过于频繁，请稍后重试");
        return;
      }
      setAlert(error.message || "请求失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  if (stage === "invalid") {
    // 失效态页：无入口按钮（审查钉死项）——仅陈述事实，出路由用户自行选择
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }} role="alert">
            <h2 className="auth-card-title">邀请链接无效或已失效</h2>
            <p className="auth-card-sub">
              该邀请不存在、已被使用或已过期。请联系管理员重新发送邀请。
            </p>
          </div>
        </section>
      </main>
    );
  }

  if (stage === "onboarding") {
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }}>
            <h2 className="auth-card-title">验证邮件已发送</h2>
            <p className="auth-card-sub">
              验证邮件已发送至 {acceptedEmail}
              ，请查收并点击邮件中的链接完成邮箱验证。验证前账户处于待验证状态。
            </p>
            {sessionConfirmed ? (
              <OnboardingResend />
            ) : (
              <>
                <div className="form-alert" role="alert">
                  请登录后在顶部横幅中重发验证邮件
                </div>
                <p className="auth-switch-hint">
                  <Link className="auth-switch-link" to="/login?v2=1">
                    前往登录
                  </Link>
                </p>
              </>
            )}
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="auth-page">
      <AuthBrandPanel />
      <section className="auth-panel">
        <div className="auth-card rise" style={{ "--rise-index": 1 }}>
          <h2 className="auth-card-title">接受邀请</h2>
          <p className="auth-card-sub">完成账户激活，开始使用你的 AgentCraft 工作台。</p>
          <form className="auth-form" onSubmit={handleSubmit} noValidate>
            <div className="form-alert" role="alert" hidden={!alert}>
              {alert}
            </div>
            <FormField label="邀请令牌">
              <input
                className="field-input v2-readonly-input"
                name="invitation_token"
                value={invitationToken}
                readOnly
                aria-readonly="true"
              />
            </FormField>
            <FormField label="邮箱" error={errors.email}>
              <input
                className="field-input"
                name="email"
                type="email"
                autoComplete="email"
                placeholder="name@example.com"
                value={fields.email}
                onChange={(event) => setField("email", event.target.value)}
              />
            </FormField>
            <FormField label="设置密码" error={errors.password}>
              <input
                className="field-input"
                name="password"
                type="password"
                autoComplete="new-password"
                placeholder="请设置密码"
                value={fields.password}
                onChange={(event) => setField("password", event.target.value)}
              />
            </FormField>
            <FormField label="确认密码" error={errors.confirmPassword}>
              <input
                className="field-input"
                name="confirmPassword"
                type="password"
                autoComplete="new-password"
                placeholder="请再次输入密码"
                value={fields.confirmPassword}
                onChange={(event) => setField("confirmPassword", event.target.value)}
              />
            </FormField>
            <div className="auth-form-footer">
              <button
                type="submit"
                className="btn btn-primary btn-block"
                disabled={submitting || retryAfter > 0}
              >
                {submitting ? "提交中…" : "激活账户"}
              </button>
              {retryAfter > 0 ? (
                <p className="v2-retry-hint" role="status">
                  操作过于频繁，请等待 {retryAfter} 秒后再试
                </p>
              ) : null}
            </div>
          </form>
        </div>
      </section>
    </main>
  );
}
