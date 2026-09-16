import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ShieldWarning } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_ACCOUNT } from "../api/v2/routes.js";
import { useRetryAfter } from "../hooks/useRetryAfter.js";
import FormField from "./FormField.jsx";
import OtpInput from "./OtpInput.jsx";

const FALLBACK_MESSAGE = "操作失败，请稍后重试";

/**
 * 注销账户危险区（FE-T7，契约 = F2 §2.2）。
 *
 * 后端契约（只读参考）：POST /api/account/deletion/request（认证端点，
 * 幂等义务端点 A5）：再认证（密码必验 + TOTP 叠加）→ 14 天宽限期；200
 * {status:"deleting", days_remaining} + 双 cookie 已清 + 全会话失效；409
 * ACCOUNT_PENDING/ACCOUNT_DELETING → 后端文案内联；401 INVALID_CREDENTIALS
 * 「当前密码不正确」；429 → Retry-After 倒计时。
 *
 * 交互流：红色语义面板（14 天宽限期说明 + V1/V2 账户域区分）→ 二次确认
 * 弹层（再认证表单：密码 + 条件 TOTP）→ 提交 → 200 后本地 V2 态清理
 * （clearV2Session：csrf 清空 + v2User 置空——后端已清双 cookie，不显式清
 * 会让陈旧态在后续请求触发 401 事件打断「注销中」页）→ navigate 独立路由
 * /account/deleting 渲染全页冻结展示态（终审修复：冻结页不挂 RequireAuth——
 * V2-only 用户清会话后匿名，挂守卫会被同批弹回 /login；days_remaining 经
 * location.state 传入）。V1 遗留 token 已由 AuthProvider 挂载时一次性清理
 * （T14 会话归一，安全审查 I-5）。
 *
 * 幂等键：useRef 初始化器（StrictMode 双渲染仅首把键保留，T5
 * DeletionCancelPage 同源裁决）+ in-flight 提交闸门；失败重试复用同一把键。
 */
export default function DangerZone() {
  const { v2User, clearV2Session } = useAuth();
  const navigate = useNavigate();
  const isMfaEnabled = v2User?.mfaEnabled === true;

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [errors, setErrors] = useState({});
  const [alert, setAlert] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  // 幂等键：挂载期生成一次，失败重试复用（T5 同源）；提交闸门防双击/竞态重入
  const idempotencyKeyRef = useRef(newIdempotencyKey());
  const inFlightRef = useRef(false);

  // Escape 关闭弹层（提交中不关闭，防误触中断受理中的请求观感）
  useEffect(() => {
    if (!confirmOpen) {
      return undefined;
    }
    function handleKeyDown(event) {
      if (event.key === "Escape" && !inFlightRef.current) {
        setConfirmOpen(false);
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [confirmOpen]);

  function openConfirm() {
    setPassword("");
    setTotpCode("");
    setErrors({});
    setAlert("");
    setConfirmOpen(true);
  }

  function closeConfirm() {
    if (inFlightRef.current) {
      return;
    }
    setConfirmOpen(false);
  }

  function handlePasswordChange(event) {
    setPassword(event.target.value);
    setErrors((current) => ({ ...current, password: undefined }));
    setAlert("");
  }

  function handleTotpChange(code) {
    setTotpCode(code);
    setErrors((current) => ({ ...current, totp: undefined }));
    setAlert("");
  }

  function validate() {
    const next = {};
    if (!password) {
      next.password = "请输入登录密码";
    }
    if (isMfaEnabled && totpCode.length < 6) {
      next.totp = "请输入 6-8 位两步验证码";
    }
    return next;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const nextErrors = validate();
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      return;
    }
    if (inFlightRef.current) {
      return;
    }
    inFlightRef.current = true;
    setIsSubmitting(true);
    setAlert("");
    try {
      const result = await requestV2(`${V2_ACCOUNT}/deletion/request`, {
        method: "POST",
        body: {
          password,
          ...(isMfaEnabled ? { totp_code: totpCode } : {}),
        },
        idempotencyKey: idempotencyKeyRef.current,
      });
      const daysRemaining = result.data?.days_remaining;
      // 本地 V2 态清理先于导航：上下文翻转（v2User=null）与路由切换同批提交，
      // 原页面（含本卡片）随之卸载，无中间态 V2 请求；冻结页不挂守卫，
      // 匿名到达 /account/deleting 不会被弹回
      clearV2Session();
      navigate("/account/deleting", { state: { daysRemaining }, replace: true });
      setConfirmOpen(false);
    } catch (caught) {
      if (caught.status === 429) {
        startRetryAfter(caught.retryAfter);
        setAlert(caught.message || "请求过于频繁，请稍后再试");
        return;
      }
      // 409 ACCOUNT_PENDING/ACCOUNT_DELETING、401 INVALID_CREDENTIALS 等
      // 均内联后端文案（V2ApiError 透传；网络层裸错误兜底）
      setAlert(caught instanceof V2ApiError ? caught.message : FALLBACK_MESSAGE);
    } finally {
      inFlightRef.current = false;
      setIsSubmitting(false);
    }
  }

  return (
    <div className="profile-card v2-danger-card rise" style={{ "--rise-index": 5 }}>
      <h2 className="profile-card-title">
        <ShieldWarning size={16} aria-hidden="true" />
        注销账户
      </h2>
      <div className="v2-danger-panel">
        <p className="v2-danger-text">
          注销 <strong>{v2User?.email}</strong> 后进入 14 天宽限期，期间可凭
          <strong>邮件中的恢复链接</strong>撤销注销；宽限期届满账户将被永久删除、
          不可恢复。本次注销仅针对新账户体系（V2 账户域），不影响旧版工作区
          账户的登录。
        </p>
        <button
          type="button"
          className="btn btn-ghost is-danger"
          onClick={openConfirm}
        >
          申请注销账户
        </button>
      </div>

      {confirmOpen ? (
        // 弹层壳复用 global.css 既有 .modal-overlay/.modal 原语（SkillEditorModal
        // 同源：遮罩点击关闭 + in-flight 守卫；reduced-motion 免动画）
        <div
          className="modal-overlay"
          onPointerDown={(event) => {
            if (event.target === event.currentTarget) {
              closeConfirm();
            }
          }}
        >
          <div
            className="modal v2-danger-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="v2-danger-dialog-title"
          >
            <h3 className="v2-danger-dialog-title" id="v2-danger-dialog-title">
              确认注销账户
            </h3>
            <p className="v2-danger-dialog-sub">
              这是不可轻易撤销的操作：注销受理后 14 天内可凭邮件中的恢复链接恢复，
              逾期账户将被永久删除。验证身份以继续。
            </p>
            <form onSubmit={handleSubmit} noValidate>
              <div className="profile-security-form">
                <FormField label="登录密码" error={errors.password}>
                  <input
                    className="field-input"
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={handlePasswordChange}
                  />
                </FormField>
                {isMfaEnabled ? (
                  <div className="field">
                    <span className="field-label">两步验证码</span>
                    <OtpInput value={totpCode} onChange={handleTotpChange} />
                    {errors.totp ? (
                      <div className="field-error" role="alert">
                        {errors.totp}
                      </div>
                    ) : null}
                  </div>
                ) : null}
                <div className="profile-security-actions">
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
                <div className="v2-danger-dialog-actions">
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={closeConfirm}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className="btn btn-primary"
                    disabled={isSubmitting || retryAfter > 0}
                  >
                    {isSubmitting ? "提交中…" : "确认注销"}
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
}
