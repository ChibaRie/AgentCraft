import { useEffect, useState } from "react";
import { ShieldCheck } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import { V2_AUTH } from "../api/v2/routes.js";
import { useRetryAfter } from "../hooks/useRetryAfter.js";
import OtpInput from "./OtpInput.jsx";

const COPY_RESET_MS = 2000;
const FALLBACK_MESSAGE = "操作失败，请稍后重试";

/**
 * 密钥复制块（FE-T6，v2.css .v2-secret-block 原语）：等宽展示全文 +
 * navigator.clipboard 复制按钮 + 「已复制/复制失败」反馈。零新依赖
 * （刻意不引 qrcode 库——base32 全文 + otpauth 链接双通道录入）。
 */
function CopyBlock({ label, text, buttonLabel }) {
  const [copyState, setCopyState] = useState("idle"); // idle | copied | failed

  useEffect(() => {
    if (copyState !== "copied") {
      return undefined;
    }
    const timer = window.setTimeout(() => setCopyState("idle"), COPY_RESET_MS);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopyState("copied");
    } catch {
      // 权限拒绝 / 非安全上下文 / 剪贴板不可用
      setCopyState("failed");
    }
  }

  return (
    <div className="v2-secret-block">
      <div className="v2-secret-body">
        <span className="v2-secret-label">{label}</span>
        <pre className="v2-secret-text">{text}</pre>
      </div>
      <button type="button" className="v2-secret-copy" onClick={handleCopy}>
        {copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : buttonLabel}
      </button>
    </div>
  );
}

/**
 * 两步验证管理卡片（FE-T6，契约 = F2 §1.9）。
 *
 * mfa_enabled 判据 = users/me（AuthContext.v2User.mfaEnabled，snake→camel 后键），
 * 挂载不发请求。未启用：setup（secret pending 10min）→ 复制块 + OtpInput →
 * activate（400 MFA_INVALID 清码内联；429 mfa_failure 倒计时）→ 200 refreshV2User
 * 上下文翻转到已启用。已启用：停用（DELETE /auth/mfa）——admin 置灰 + title
 * （A9 后端 403 FORBIDDEN 兜底内联）。
 */
export default function MfaCard() {
  const { v2User, refreshV2User } = useAuth();
  const isAdmin = v2User?.role === "admin";
  const isMfaEnabled = v2User?.mfaEnabled === true;

  // setup 面板数据（{secret, otpauthUri}）：仅客户端暂存，pending secret 服务端 10min 过期
  const [setup, setSetup] = useState(null);
  const [totpCode, setTotpCode] = useState("");
  const [alert, setAlert] = useState("");
  const [busy, setBusy] = useState(""); // "" | "setup" | "activate" | "disable"
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  function clearAlert() {
    setAlert("");
  }

  function handleTotpChange(code) {
    clearAlert();
    setTotpCode(code);
  }

  async function handleSetup() {
    clearAlert();
    setBusy("setup");
    try {
      const result = await requestV2(`${V2_AUTH}/mfa/setup`, { method: "POST" });
      setSetup({
        secret: result.data?.secret ?? "",
        otpauthUri: result.data?.otpauth_uri ?? "",
      });
      setTotpCode("");
    } catch (caught) {
      setAlert(caught instanceof V2ApiError ? caught.message : FALLBACK_MESSAGE);
    } finally {
      setBusy("");
    }
  }

  async function handleActivate() {
    clearAlert();
    setBusy("activate");
    try {
      await requestV2(`${V2_AUTH}/mfa/activate`, {
        method: "POST",
        body: { totp_code: totpCode },
      });
      setSetup(null);
      setTotpCode("");
      await refreshV2User();
    } catch (caught) {
      if (caught.status === 429) {
        startRetryAfter(caught.retryAfter);
        setAlert(caught.message || "请求过于频繁，请稍后再试");
        return;
      }
      setAlert(caught instanceof V2ApiError ? caught.message : FALLBACK_MESSAGE);
      if (caught.code === "MFA_INVALID") {
        setTotpCode(""); // LoginPage MFA_INVALID 清码裁决同源
      }
    } finally {
      setBusy("");
    }
  }

  async function handleDisable() {
    clearAlert();
    setBusy("disable");
    try {
      await requestV2(`${V2_AUTH}/mfa`, { method: "DELETE" });
      await refreshV2User();
    } catch (caught) {
      // 403 FORBIDDEN「管理员不可停用 TOTP」兜底（正常路径 admin 已置灰）
      setAlert(caught instanceof V2ApiError ? caught.message : FALLBACK_MESSAGE);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="profile-card rise" style={{ "--rise-index": 4 }}>
      <h2 className="profile-card-title">
        <ShieldCheck size={16} aria-hidden="true" />
        两步验证
      </h2>
      {isMfaEnabled ? (
        <>
          <span className="profile-security-status">已启用两步验证</span>
          <p className="profile-expert-desc">
            登录与修改密码时，除密码外还需输入验证器 App 生成的动态码。
          </p>
          <div className="profile-security-actions">
            {alert ? (
              <p className="form-alert" role="alert">
                {alert}
              </p>
            ) : null}
            <button
              type="button"
              className="btn btn-ghost is-danger"
              disabled={isAdmin || busy === "disable"}
              title={isAdmin ? "管理员不可停用两步验证" : undefined}
              onClick={handleDisable}
            >
              {busy === "disable" ? "停用中…" : "停用"}
            </button>
          </div>
        </>
      ) : setup ? (
        <>
          <p className="profile-expert-desc">
            在验证器 App 中添加以下密钥（手动录入或粘贴 otpauth 链接），然后输入其显示的
            6-8 位动态码完成启用。
          </p>
          <div className="profile-security-form">
            <CopyBlock label="密钥" text={setup.secret} buttonLabel="复制密钥" />
            <CopyBlock label="otpauth 链接" text={setup.otpauthUri} buttonLabel="复制链接" />
            <div className="field">
              <span className="field-label">两步验证码</span>
              <OtpInput value={totpCode} onChange={handleTotpChange} />
            </div>
            <div className="profile-security-actions">
              {alert ? (
                <p className="form-alert" role="alert">
                  {alert}
                </p>
              ) : null}
              <button
                type="button"
                className="btn btn-primary"
                disabled={
                  totpCode.length < 6 || busy === "activate" || retryAfter > 0
                }
                onClick={handleActivate}
              >
                {busy === "activate" ? "验证中…" : "确认启用"}
              </button>
              {retryAfter > 0 ? (
                <p className="v2-retry-hint" role="status">
                  操作过于频繁，请等待 {retryAfter} 秒后再试
                </p>
              ) : null}
            </div>
          </div>
        </>
      ) : (
        <>
          <p className="profile-expert-desc">
            为账户添加第二重保护：启用后，登录与修改密码时除密码外还需输入验证器
            App 生成的动态码。
          </p>
          <div className="profile-security-actions">
            {alert ? (
              <p className="form-alert" role="alert">
                {alert}
              </p>
            ) : null}
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy === "setup"}
              onClick={handleSetup}
            >
              {busy === "setup" ? "生成中…" : "开始设置"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
