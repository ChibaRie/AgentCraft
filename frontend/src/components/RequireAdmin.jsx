import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { ShieldCheck } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import { V2_AUTH } from "../api/v2/routes.js";
import { useRetryAfter } from "../hooks/useRetryAfter.js";
import MfaCard from "./MfaCard.jsx";
import OtpInput from "./OtpInput.jsx";

const FALLBACK_MESSAGE = "操作失败，请稍后重试";

const AdminGateContext = createContext(null);

/** 领地内页面消费：403 数据面上报入口（RequireAdmin 提供的 gate API） */
export function useAdminGate() {
  const context = useContext(AdminGateContext);
  if (!context) {
    throw new Error("useAdminGate 必须在 RequireAdmin 内使用");
  }
  return context;
}

/**
 * TOTP step-up 续期卡（12h 窗过期分支，mfaEnabled=true）：POST /auth/mfa/verify
 * （Sup §10.4，T3 端点）→ 200 {mfa_verified:true} 后门③即刻解除，onVerified
 * 由壳层清门并重放原请求。401 MFA_INVALID 内联；400 MFA_NOT_CONFIGURED 说明
 * 本地 mfaEnabled 陈旧 → 降级注册引导；429 沿 mfa_failure 限流倒计时。
 */
function AdminMfaVerifyCard({ onVerified, onNotConfigured }) {
  const [totpCode, setTotpCode] = useState("");
  const [alert, setAlert] = useState("");
  const [busy, setBusy] = useState(false);
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  async function handleVerify() {
    setAlert("");
    setBusy(true);
    try {
      await requestV2(`${V2_AUTH}/mfa/verify`, {
        method: "POST",
        body: { totp_code: totpCode },
      });
      onVerified();
    } catch (caught) {
      if (caught instanceof V2ApiError && caught.code === "MFA_NOT_CONFIGURED") {
        onNotConfigured();
        return;
      }
      if (caught.status === 429) {
        startRetryAfter(caught.retryAfter);
        setAlert(caught.message || "请求过于频繁，请稍后再试");
        return;
      }
      setAlert(caught instanceof V2ApiError ? caught.message : FALLBACK_MESSAGE);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="profile-card rise" style={{ "--rise-index": 1 }}>
      <h2 className="profile-card-title">
        <ShieldCheck size={16} aria-hidden="true" />
        管理员身份验证
      </h2>
      <p className="profile-expert-desc">
        管理员会话的两步验证时效已过期。请输入验证器 App 当前显示的动态码以继续管理操作。
      </p>
      <div className="profile-security-form">
        <div className="field">
          <span className="field-label">两步验证码</span>
          <OtpInput
            value={totpCode}
            onChange={(code) => {
              setAlert("");
              setTotpCode(code);
            }}
          />
        </div>
        {alert ? (
          <p className="form-alert" role="alert">
            {alert}
          </p>
        ) : null}
        <div className="profile-security-actions">
          <button
            type="button"
            className="btn btn-primary"
            disabled={totpCode.length < 6 || busy || retryAfter > 0}
            onClick={handleVerify}
          >
            {busy ? "验证中…" : "验证并继续"}
          </button>
          {retryAfter > 0 ? (
            <p className="v2-retry-hint" role="status">
              操作过于频繁，请等待 {retryAfter} 秒后再试
            </p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

/**
 * admin 路由守卫（Phase 8 T12a；勘察报告 §3.3/§3.4 + R5/R9）。
 *
 * 软门（免探测往返）：
 * - authReady 未落定渲染 null；`v2User?.role !== 'admin'` → Navigate /
 *   （admin 判据仅 V2 role；V1 role 与此无关，RequireAuth 的双轨逻辑不适用）。
 * - **不预检 MFA**：12h 时效无查询端点，交由数据请求 403 数据面分流。
 *
 * 硬门（403 同 HTTP 按 error.code 分流，R5）——页面经 useAdminGate().reportAdminError
 * 上报，返回 true 表示已接管（页面不再自行展示错误）：
 * - `FORBIDDEN`（角色不符/已撤销，R9）：refreshV2User 重探测清陈旧 role +
 *   Navigate /（服务端 403 为权威，本地软门仅为体验）；
 * - `ADMIN_MFA_REQUIRED`（未配置/12h 过期两因子同 code 不可区分）：以
 *   v2User.mfaEnabled 分流——false → 注册引导（mode=enroll，内嵌 MfaCard，
 *   setup+activate 即盖当前会话戳，激活翻转上下文后自动重放原请求——bootstrap
 *   首登免重登录）；true → TOTP verify 卡（mode=verify，§10.4 step-up 续期）
 *   成功后重放。verify 返回 400 MFA_NOT_CONFIGURED 说明本地 mfaEnabled 陈旧
 *   （admin 不可停用 TOTP，A9——重探测收敛后仍落注册引导）。
 *   重放闭包由页面提供（同初次调用路径、自行捕获业务错误）。
 */
export default function RequireAdmin({ children }) {
  const { authReady, v2User, refreshV2User } = useAuth();
  // gate：null | {code:"FORBIDDEN"} | {code:"ADMIN_MFA_REQUIRED", mode:"enroll"|"verify"}
  const [gate, setGate] = useState(null);
  const replayRef = useRef(null);
  const isMfaEnabled = v2User?.mfaEnabled === true;

  const completeGate = useCallback(() => {
    const replay = replayRef.current;
    replayRef.current = null;
    setGate(null);
    // 重放契约：replay 走页面自身加载路径（内部捕获业务错误），与初次调用同形
    replay?.();
  }, []);

  const reportAdminError = useCallback(
    (error, replay) => {
      if (!(error instanceof V2ApiError) || error.status !== 403) {
        return false;
      }
      if (error.code === "FORBIDDEN") {
        replayRef.current = null;
        setGate({ code: "FORBIDDEN" });
        // R9：本地 v2User.role 已陈旧——重探测以服务端为准（探测内部置位/置空）
        refreshV2User();
        return true;
      }
      if (error.code === "ADMIN_MFA_REQUIRED") {
        replayRef.current = typeof replay === "function" ? replay : null;
        // 新 episode：重置「观察到未配置」标记（激活翻转信号按 episode 判定）
        mfaWasFalseRef.current = false;
        // 已配置者弹 verify 卡（12h 过期）；未配置/未知 → 注册引导
        setGate({
          code: "ADMIN_MFA_REQUIRED",
          mode: v2User?.mfaEnabled === true ? "verify" : "enroll",
        });
        return true;
      }
      return false;
    },
    [refreshV2User, v2User]
  );

  const gateApi = useMemo(() => ({ reportAdminError }), [reportAdminError]);

  // 「会话内观察到未配置」标记：仅在 episode 内 mfaEnabled 实际出现过 false，
  // 其后的 true 翻转才是 activate 完成的信号——MFA_NOT_CONFIGURED 降级窗口
  // （本地 mfaEnabled 陈旧为 true）不得触发自动重放，防止 403→verify→降级 环路。
  const mfaWasFalseRef = useRef(false);

  // 注册引导完成监听：MfaCard activate 成功 → refreshV2User 翻转 mfaEnabled →
  // 当前会话已盖 mfa_verified_at 戳 → 清门并重放原请求。MFA_NOT_CONFIGURED
  // 分支的重探测先把陈旧的 mfaEnabled=true 收敛为服务端实态（false），随后
  // setup+activate 的真实翻转才推进本监听。
  useEffect(() => {
    if (gate?.code === "ADMIN_MFA_REQUIRED" && !isMfaEnabled) {
      mfaWasFalseRef.current = true;
    }
  }, [gate, isMfaEnabled]);

  useEffect(() => {
    if (
      gate?.code === "ADMIN_MFA_REQUIRED" &&
      gate.mode === "enroll" &&
      isMfaEnabled &&
      mfaWasFalseRef.current
    ) {
      completeGate();
    }
  }, [gate, isMfaEnabled, completeGate]);

  if (!authReady) {
    return null;
  }
  if (v2User?.role !== "admin" || gate?.code === "FORBIDDEN") {
    return <Navigate to="/" replace />;
  }

  if (gate?.code === "ADMIN_MFA_REQUIRED" && gate.mode === "enroll" && !isMfaEnabled) {
    return (
      <AdminGateContext.Provider value={gateApi}>
        <main className="app-main">
          <div className="profile-card rise" style={{ "--rise-index": 0 }}>
            <h2 className="profile-card-title">启用两步验证</h2>
            <p className="profile-expert-desc">
              管理员操作要求会话持有 12 小时内的两步验证记录，而该账户尚未配置
              TOTP。请在下方完成注册（激活后当前会话立即生效，无需重新登录）。
            </p>
          </div>
          <MfaCard />
        </main>
      </AdminGateContext.Provider>
    );
  }

  if (gate?.code === "ADMIN_MFA_REQUIRED") {
    return (
      <AdminGateContext.Provider value={gateApi}>
        <main className="app-main">
          <AdminMfaVerifyCard
            onVerified={completeGate}
            onNotConfigured={() => {
              // 本地 mfaEnabled 陈旧（服务端实态未配置）：重探测收敛 + 转注册引导
              setGate((current) =>
                current?.code === "ADMIN_MFA_REQUIRED"
                  ? { ...current, mode: "enroll" }
                  : current
              );
              refreshV2User();
            }}
          />
        </main>
      </AdminGateContext.Provider>
    );
  }

  return <AdminGateContext.Provider value={gateApi}>{children}</AdminGateContext.Provider>;
}
