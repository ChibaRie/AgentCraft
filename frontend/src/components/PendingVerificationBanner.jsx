import { useAuth } from "../auth/AuthContext.jsx";
import { useResendVerification } from "../hooks/useResendVerification.js";

/**
 * pending 全局横幅（FE-T4）：v2User.status === "pending" 时渲染于 NavBar 下方
 * （AppShell 内接线，/login 等壳外全屏页不渲染——横幅语义跟随 NavBar）。
 * 重发按钮：认证端点（requestV2 自动 CSRF）+ 60s 前端冷却 + 429 服务端倒计时。
 */
export default function PendingVerificationBanner() {
  const { v2User } = useAuth();
  const { resend, cooldown, sending, sent, error } = useResendVerification();

  if (v2User?.status !== "pending") {
    return null;
  }

  return (
    <div className="v2-pending-banner">
      <span className="v2-pending-banner-dot" aria-hidden="true" />
      <strong className="v2-pending-banner-title">邮箱尚未验证</strong>
      <span className="v2-pending-banner-text">请查收验证邮件，验证后即可正常使用。</span>
      {sent ? (
        <span className="v2-pending-banner-note" role="status">
          验证邮件已重发，请查收
        </span>
      ) : null}
      {error ? (
        <span className="v2-pending-banner-error" role="alert">
          {error}
        </span>
      ) : null}
      <button
        type="button"
        className="btn btn-ghost v2-pending-banner-resend"
        onClick={resend}
        disabled={sending || cooldown > 0}
      >
        {cooldown > 0 ? `重新发送（${cooldown}s）` : sending ? "发送中…" : "重发验证邮件"}
      </button>
    </div>
  );
}
