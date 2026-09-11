import { useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_AUTH } from "../api/v2/routes.js";
import AuthBrandPanel from "../components/AuthBrandPanel.jsx";
import FormField from "../components/FormField.jsx";

/**
 * 密码重置确认落地页（FE-T5，邮件链接 ?token= 直达）：
 * - 公开令牌流（E11）：不挂守卫，独立于应用壳（P01 全屏构图，同 /login）。
 * - 新密码 + 确认（前端一致性校验）→ `POST /auth/password-reset/confirm`
 *   （公开端点，无会话无 CSRF；幂等义务端点 A5）。
 * - 幂等键（StrictMode 陷阱，T4 同款）：useRef 初始化器生成——StrictMode 双渲染
 *   仅首把键保留；同一令牌的重试复用同一把键（同一逻辑操作，服务端可安全重放）。
 *   提交闸门（in-flight latch）挡双击/并发重入；用户事件本身不受 StrictMode
 *   双渲染影响，故 confirm 恒单发。
 * - 200 → 成功页「密码已重置，请重新登录」→ 前往登录（全会话已失效，强制重登）；
 *   400 EMAIL_NOT_VERIFIED → 失效页（后端全部失效形态统一此码此文案，防探测）；
 *   其它失败 → 内联 alert 留在表单，重试复用同一把幂等键。
 */

export default function PasswordResetConfirmPage() {
  const [searchParams] = useSearchParams();

  const resetToken = searchParams.get("token") || "";
  // 态机：form（表单）→ success | invalid；query 无 token 直接落失效页（不发无意义请求）
  const [stage, setStage] = useState(() => (resetToken ? "form" : "invalid"));
  const [fields, setFields] = useState({ password: "", confirmPassword: "" });
  const [errors, setErrors] = useState({});
  const [alert, setAlert] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // useRef 初始化器：StrictMode 双渲染下仅首把键保留（生成两次、使用一次）；
  // 失败重试复用同一把键
  const idempotencyKeyRef = useRef(newIdempotencyKey());
  // 提交闸门：in-flight 期间忽略重入提交（双击/竞态）
  const inFlightRef = useRef(false);

  function setField(name, value) {
    setFields((current) => ({ ...current, [name]: value }));
    setErrors((current) => ({ ...current, [name]: "" }));
    setAlert("");
  }

  async function runConfirm() {
    if (inFlightRef.current) {
      return;
    }
    inFlightRef.current = true;
    setSubmitting(true);
    setAlert("");
    try {
      await requestV2(`${V2_AUTH}/password-reset/confirm`, {
        method: "POST",
        body: { reset_token: resetToken, new_password: fields.password },
        idempotencyKey: idempotencyKeyRef.current,
      });
      setStage("success");
    } catch (caught) {
      if (caught.code === "EMAIL_NOT_VERIFIED") {
        setStage("invalid");
        return;
      }
      setAlert(caught.message || "请求失败，请稍后重试");
    } finally {
      inFlightRef.current = false;
      setSubmitting(false);
    }
  }

  function handleSubmit(event) {
    event.preventDefault();
    const nextErrors = {
      password: fields.password ? "" : "请设置新密码",
      confirmPassword: fields.confirmPassword ? "" : "请再次输入新密码",
    };
    if (!nextErrors.confirmPassword && fields.confirmPassword !== fields.password) {
      nextErrors.confirmPassword = "两次输入的密码不一致";
    }
    setErrors(nextErrors);
    if (nextErrors.password || nextErrors.confirmPassword || !resetToken) {
      return;
    }
    runConfirm();
  }

  if (stage === "invalid") {
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }} role="alert">
            <h2 className="auth-card-title">链接无效或已过期</h2>
            <p className="auth-card-sub">
              该重置链接不存在、已被使用或已过期。请重新发起密码重置。
            </p>
            <p className="auth-switch-hint">
              <Link className="auth-switch-link" to="/password-reset">
                重新发起重置
              </Link>
              <span aria-hidden="true">　·　</span>
              <Link className="auth-switch-link" to="/login?v2=1">
                前往登录
              </Link>
            </p>
          </div>
        </section>
      </main>
    );
  }

  if (stage === "success") {
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }}>
            <h2 className="auth-card-title">密码已重置，请重新登录</h2>
            <p className="auth-card-sub">
              为保障安全，重置后所有登录会话均已失效，请使用新密码重新登录。
            </p>
            <div className="auth-form-footer">
              <Link className="btn btn-primary btn-block" to="/login?v2=1">
                前往登录
              </Link>
            </div>
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
          <h2 className="auth-card-title">设置新密码</h2>
          <p className="auth-card-sub">为你的 AgentCraft 账户设置新密码。</p>
          <form className="auth-form" onSubmit={handleSubmit} noValidate>
            <div className="form-alert" role="alert" hidden={!alert}>
              {alert}
            </div>
            <FormField label="新密码" error={errors.password}>
              <input
                className="field-input"
                name="password"
                type="password"
                autoComplete="new-password"
                placeholder="请设置新密码"
                value={fields.password}
                onChange={(event) => setField("password", event.target.value)}
              />
            </FormField>
            <FormField label="确认新密码" error={errors.confirmPassword}>
              <input
                className="field-input"
                name="confirmPassword"
                type="password"
                autoComplete="new-password"
                placeholder="请再次输入新密码"
                value={fields.confirmPassword}
                onChange={(event) => setField("confirmPassword", event.target.value)}
              />
            </FormField>
            <div className="auth-form-footer">
              <button
                type="submit"
                className="btn btn-primary btn-block"
                disabled={submitting}
              >
                {submitting ? "提交中…" : "重置密码"}
              </button>
            </div>
          </form>
        </div>
      </section>
    </main>
  );
}
