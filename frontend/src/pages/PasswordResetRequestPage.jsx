import { useState } from "react";
import { Link } from "react-router-dom";
import { requestV2 } from "../api/v2/client.js";
import { V2_AUTH } from "../api/v2/routes.js";
import AuthBrandPanel from "../components/AuthBrandPanel.jsx";
import FormField from "../components/FormField.jsx";
import { useRetryAfter } from "../hooks/useRetryAfter.js";

/**
 * 密码重置请求落地页（FE-T5）：
 * - 公开令牌流（E11）：不挂守卫，独立于应用壳（P01 全屏构图，同 /login）。
 * - `POST /auth/password-reset/request`（无 CSRF 无幂等，A5 未列入键控）。
 * - 202 防枚举语义：**恒展示**「如该邮箱存在，重置链接已发送」——响应不含存在性
 *   信息，前端不得区分账号是否存在（测试钉死）。
 * - 429 → Retry-After 秒倒计时禁用提交；其它错误直显后端中文文案，停留表单。
 */

export default function PasswordResetRequestPage() {
  // 态机：form（表单）→ sent（恒定文案确认页，无再发入口——重试由用户回登录页重进）
  const [stage, setStage] = useState("form");
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [alert, setAlert] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  function setField(value) {
    setEmail(value);
    setError("");
    setAlert("");
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const trimmed = email.trim();
    if (!trimmed) {
      setError("请输入邮箱");
      return;
    }
    setSubmitting(true);
    setAlert("");
    try {
      await requestV2(`${V2_AUTH}/password-reset/request`, {
        method: "POST",
        body: { email: trimmed },
      });
      setStage("sent");
    } catch (caught) {
      if (caught.status === 429) {
        startRetryAfter(caught.retryAfter);
        setAlert(caught.message || "请求过于频繁，请稍后重试");
        return;
      }
      setAlert(caught.message || "请求失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  if (stage === "sent") {
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }}>
            <h2 className="auth-card-title">请查收邮件</h2>
            {/* 文案为契约钉死（202 防枚举恒定形态），保持逐字一致便于测试断言 */}
            <p className="auth-card-sub">如该邮箱存在，重置链接已发送</p>
            <p className="auth-switch-hint">
              <Link className="auth-switch-link" to="/login?v2=1">
                返回登录
              </Link>
            </p>
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
          <h2 className="auth-card-title">重置密码</h2>
          <p className="auth-card-sub">输入注册邮箱，我们会发送重置链接到你的邮箱。</p>
          <form className="auth-form" onSubmit={handleSubmit} noValidate>
            <div className="form-alert" role="alert" hidden={!alert}>
              {alert}
            </div>
            <FormField label="邮箱" error={error}>
              <input
                className="field-input"
                name="email"
                type="email"
                autoComplete="email"
                placeholder="name@example.com"
                value={email}
                onChange={(event) => setField(event.target.value)}
              />
            </FormField>
            <div className="auth-form-footer">
              <button
                type="submit"
                className="btn btn-primary btn-block"
                disabled={submitting || retryAfter > 0}
              >
                {submitting ? "发送中…" : "发送重置链接"}
              </button>
              {retryAfter > 0 ? (
                <p className="v2-retry-hint" role="status">
                  操作过于频繁，请等待 {retryAfter} 秒后再试
                </p>
              ) : null}
              <p className="auth-switch-hint">
                <Link className="auth-switch-link" to="/login?v2=1">
                  返回登录
                </Link>
              </p>
            </div>
          </form>
        </div>
      </section>
    </main>
  );
}
