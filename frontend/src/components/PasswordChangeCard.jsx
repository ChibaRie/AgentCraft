import { useState } from "react";
import { LockKey } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import { V2_AUTH } from "../api/v2/routes.js";
import { useRetryAfter } from "../hooks/useRetryAfter.js";
import FormField from "./FormField.jsx";
import OtpInput from "./OtpInput.jsx";

const FALLBACK_MESSAGE = "修改失败，请稍后重试";

/**
 * 密码修改卡片（FE-T6，契约 = F2 §1.8）。
 *
 * 后端契约（只读参考）：POST /api/auth/password-change（认证端点；A7 未列
 * 幂等义务——无幂等键）body {current_password, new_password, totp_code?}；
 * 200 {ok:true}（其余会话被撤销，当前会话保留）→ 提示 + refreshV2User；
 * 400 MFA_INVALID「验证码无效」/ 401 INVALID_CREDENTIALS「当前密码不正确」→
 * 内联后端文案；429 password_change_totp → Retry-After 倒计时。
 *
 * TOTP 条件性：v2User.mfaEnabled === true（users/me 判据，snake→camel 后键）
 * 时显示且必填；其余用户不渲染该字段、body 不携带 totp_code 键。
 */
export default function PasswordChangeCard() {
  const { v2User, refreshV2User } = useAuth();
  const isMfaEnabled = v2User?.mfaEnabled === true;

  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [errors, setErrors] = useState({});
  const [alert, setAlert] = useState("");
  const [succeeded, setSucceeded] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  function clearFeedback() {
    setAlert("");
    setSucceeded(false);
  }

  function validate() {
    const next = {};
    if (!currentPassword) {
      next.current = "请输入当前密码";
    }
    if (!newPassword) {
      next.next = "请输入新密码";
    }
    if (!confirmPassword) {
      next.confirm = "请再次输入新密码";
    } else if (newPassword && newPassword !== confirmPassword) {
      next.confirm = "两次输入的新密码不一致";
    }
    if (isMfaEnabled && totpCode.length < 6) {
      next.totp = "请输入 6-8 位两步验证码";
    }
    return next;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    clearFeedback();
    const nextErrors = validate();
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      return;
    }
    setErrors({});
    setIsSubmitting(true);
    try {
      // A7 契约缺口补端点：非幂等义务端点——不传 idempotencyKey
      await requestV2(`${V2_AUTH}/password-change`, {
        method: "POST",
        body: {
          current_password: currentPassword,
          new_password: newPassword,
          ...(isMfaEnabled ? { totp_code: totpCode } : {}),
        },
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setTotpCode("");
      setSucceeded(true);
      await refreshV2User();
    } catch (caught) {
      if (caught.status === 429) {
        startRetryAfter(caught.retryAfter);
        setAlert(caught.message || "请求过于频繁，请稍后再试");
        return;
      }
      // V2ApiError 透传后端文案；网络层裸错误用兜底（useResendVerification 同裁决）
      setAlert(caught instanceof V2ApiError ? caught.message : FALLBACK_MESSAGE);
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="profile-card rise" style={{ "--rise-index": 3 }}>
      <h2 className="profile-card-title">
        <LockKey size={16} aria-hidden="true" />
        密码修改
      </h2>
      <form onSubmit={handleSubmit} noValidate>
        <div className="profile-security-form">
          <FormField label="当前密码" error={errors.current}>
            <input
              className="field-input"
              type="password"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
            />
          </FormField>
          <FormField label="新密码" error={errors.next}>
            <input
              className="field-input"
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
            />
          </FormField>
          <FormField label="确认新密码" error={errors.confirm}>
            <input
              className="field-input"
              type="password"
              autoComplete="new-password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
            />
          </FormField>
          {isMfaEnabled ? (
            <div className="field">
              <span className="field-label">两步验证码</span>
              <OtpInput value={totpCode} onChange={setTotpCode} />
              {errors.totp ? (
                <div className="field-error" role="alert">
                  {errors.totp}
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="profile-security-actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={isSubmitting || retryAfter > 0}
            >
              {isSubmitting ? "提交中…" : "修改密码"}
            </button>
            {succeeded ? (
              <p className="form-alert" role="status">
                密码已修改，其他设备已退出登录
              </p>
            ) : null}
            {alert ? (
              <p className="form-alert" role="alert">
                {alert}
              </p>
            ) : null}
            {retryAfter > 0 ? (
              <p className="v2-retry-hint" role="status">
                操作过于频繁，请等待 {retryAfter} 秒后再试
              </p>
            ) : null}
          </div>
        </div>
      </form>
    </div>
  );
}
