import { useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { isSafeInternalPath, useAuth, V2_LOGIN_FROM_STORAGE_KEY } from "../auth/AuthContext.jsx";
import AuthBrandPanel from "../components/AuthBrandPanel.jsx";
import FormField from "../components/FormField.jsx";
import OtpInput from "../components/OtpInput.jsx";
import { useRetryAfter } from "../hooks/useRetryAfter.js";

/**
 * 读取并消费 401 会话过期时暂存的来源路径（D13/I-4）：消费即清除（防陈旧值
 * 滞留到下次登录）；读侧同白名单过滤（isSafeInternalPath）——存储可被篡改或
 * 存历史遗留脏值，`//evil.com` 协议相对串必须拒绝、回落 /。
 */
function readLoginFrom() {
  try {
    const raw = window.sessionStorage.getItem(V2_LOGIN_FROM_STORAGE_KEY);
    window.sessionStorage.removeItem(V2_LOGIN_FROM_STORAGE_KEY);
    if (isSafeInternalPath(raw)) {
      return raw;
    }
  } catch {
    // sessionStorage 不可用 → 忽略（回落 location.state?.from ?? "/"）
  }
  return null;
}

function RetryHint({ retryAfter }) {
  if (retryAfter <= 0) {
    return null;
  }
  return (
    <p className="v2-retry-hint" role="status">
      操作过于频繁，请等待 {retryAfter} 秒后再试
    </p>
  );
}

/**
 * 登录页双轨重写（FE-T3）：
 * - 双 Tab：「工作区登录」= V1 既有登录逻辑原样（token 存 localStorage + login()）；
 *   「账户登录」= V2（loginV2 → mfa_required 就地切换挑战卡片 → loginV2Mfa）。
 * - URL ?v2=1（V2 会话过期 401 事件跳转落点）默认激活 V2 Tab；V2 登录成功
 *   （含 MFA 挑战完成路径）恢复 401 时 sessionStorage 暂存的 from（D13/I-4：
 *   白名单站内根相对路径、消费即清除、恢复一律 navigate()），回落 state.from，再回落 /。
 * - E5：注册 Tab 删除（V2 注册走邀请制，不在登录页）。
 * - E11：/login 不设路由守卫——双轨 R1 下任一单侧会话用户都可能经 /login
 *   补建另一轨会话，已登录访问不弹回。
 * - 简单性裁决：双 Tab 切换不保留另一 Tab 的表单态（两侧瞬时态全部复位）。
 */
export default function LoginPage() {
  const { login, loginV2, loginV2Mfa } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  const [activeTab, setActiveTab] = useState(() =>
    searchParams.get("v2") === "1" ? "v2" : "v1"
  );

  // V1 表单态（原 LoginPage 登录分支原样搬入）
  const [v1Fields, setV1Fields] = useState({ login: "", password: "" });
  const [v1Errors, setV1Errors] = useState({});
  const [v1Alert, setV1Alert] = useState("");
  const [v1Submitting, setV1Submitting] = useState(false);

  // V2 态机：form（表单）→ challenge（MFA 挑战）| blocked（403 全页提示）
  const [v2Stage, setV2Stage] = useState("form");
  const [v2Fields, setV2Fields] = useState({ email: "", password: "" });
  const [v2Errors, setV2Errors] = useState({});
  const [v2Alert, setV2Alert] = useState("");
  const [v2Submitting, setV2Submitting] = useState(false);
  const [challengeId, setChallengeId] = useState(null);
  const [otp, setOtp] = useState("");
  const [blockedMessage, setBlockedMessage] = useState("");

  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  function setV1Field(name, value) {
    setV1Fields((current) => ({ ...current, [name]: value }));
    setV1Errors((current) => ({ ...current, [name]: "" }));
    setV1Alert("");
  }

  function setV2Field(name, value) {
    setV2Fields((current) => ({ ...current, [name]: value }));
    setV2Errors((current) => ({ ...current, [name]: "" }));
    setV2Alert("");
  }

  function handleOtpChange(next) {
    setOtp(next);
    setV2Alert("");
  }

  function switchTab(nextTab) {
    if (nextTab === activeTab) {
      return;
    }
    // 切换不保留另一 Tab 的表单态：两侧瞬时态全部复位（测试钉死）
    setActiveTab(nextTab);
    setV1Fields({ login: "", password: "" });
    setV1Errors({});
    setV1Alert("");
    setV2Stage("form");
    setV2Fields({ email: "", password: "" });
    setV2Errors({});
    setV2Alert("");
    setChallengeId(null);
    setOtp("");
    setBlockedMessage("");
  }

  /** 「返回重新登录」：仅重置 MFA 挑战态机，保留已输入凭据 */
  function resetV2Challenge() {
    setV2Stage("form");
    setChallengeId(null);
    setOtp("");
    setV2Alert("");
  }

  /** 403 全页提示态的返回：V2 态机整体复位（含表单字段） */
  function resetBlockedState() {
    setV2Stage("form");
    setBlockedMessage("");
    setChallengeId(null);
    setOtp("");
    setV2Alert("");
    setV2Errors({});
    setV2Fields({ email: "", password: "" });
  }

  async function handleV1Login(event) {
    event.preventDefault();
    const nextErrors = {
      login: v1Fields.login.trim() ? "" : "请输入用户名或邮箱",
      password: v1Fields.password ? "" : "请输入密码",
    };
    setV1Errors(nextErrors);
    if (nextErrors.login || nextErrors.password) {
      return;
    }
    setV1Submitting(true);
    try {
      await login(v1Fields.login, v1Fields.password);
      navigate(location.state?.from || "/", { replace: true });
    } catch (error) {
      setV1Alert(error.message || "请求失败，请稍后重试");
    } finally {
      setV1Submitting(false);
    }
  }

  /**
   * V2 错误分流（契约：401 内联 + 清空对应输入 / 429 倒计时禁用 / 403 全页提示）。
   * login 与 loginV2Mfa 共用。
   */
  function applyV2Error(error) {
    if (error.code === "ACCOUNT_SUSPENDED" || error.code === "ACCOUNT_DELETING") {
      setBlockedMessage(error.message || "账户当前不可用");
      setV2Stage("blocked");
      return;
    }
    if (error.status === 429) {
      startRetryAfter(error.retryAfter);
      setV2Alert(error.message || "请求过于频繁，请稍后重试");
      return;
    }
    if (error.status === 401) {
      // 清空对应输入：凭据错清密码（保留邮箱便于改重试），挑战码错清验证码
      if (error.code === "INVALID_CREDENTIALS") {
        setV2Fields((current) => ({ ...current, password: "" }));
      } else if (error.code === "MFA_INVALID") {
        setOtp("");
      }
    }
    setV2Alert(error.message || "请求失败，请稍后重试");
  }

  async function handleV2Login(event) {
    event.preventDefault();
    const email = v2Fields.email.trim();
    const nextErrors = {
      email: email ? "" : "请输入邮箱",
      password: v2Fields.password ? "" : "请输入密码",
    };
    setV2Errors(nextErrors);
    if (nextErrors.email || nextErrors.password) {
      return;
    }
    setV2Submitting(true);
    try {
      const result = await loginV2(email, v2Fields.password);
      if (result.mfaRequired) {
        // 就地切换挑战卡片（不离开 /login；from 暂存留在原地，由挑战完成路径消费）
        setChallengeId(result.challengeId);
        setOtp("");
        setV2Alert("");
        setV2Stage("challenge");
        return;
      }
      // D13/I-4：优先消费 401 暂存 from（v2:session-expired 全页跳转丢 router
      // state 的恢复通道），回落 state.from，再回落 /；恢复一律 navigate()
      //（禁 location.assign）
      navigate(readLoginFrom() ?? location.state?.from ?? "/", { replace: true });
    } catch (error) {
      applyV2Error(error);
    } finally {
      setV2Submitting(false);
    }
  }

  async function handleV2Mfa(event) {
    event.preventDefault();
    if (!challengeId) {
      return;
    }
    setV2Submitting(true);
    try {
      await loginV2Mfa(challengeId, otp);
      // MFA 挑战完成路径同享 from 恢复（暂存未被 mfa_required 分支消费）
      navigate(readLoginFrom() ?? location.state?.from ?? "/", { replace: true });
    } catch (error) {
      applyV2Error(error);
    } finally {
      setV2Submitting(false);
    }
  }

  if (v2Stage === "blocked") {
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }} role="alert">
            <h2 className="auth-card-title">账户当前不可用</h2>
            <p className="auth-card-sub">{blockedMessage}</p>
            <button
              type="button"
              className="btn btn-ghost btn-block"
              onClick={resetBlockedState}
            >
              返回登录
            </button>
          </div>
        </section>
      </main>
    );
  }

  const isV1Tab = activeTab === "v1";

  return (
    <main className="auth-page">
      <AuthBrandPanel />

      <section className="auth-panel">
        <div className="auth-card rise" style={{ "--rise-index": 1 }}>
          <div className="v2-tabs" data-active={activeTab} role="group" aria-label="登录方式">
            <span className="v2-tabs-thumb" aria-hidden="true" />
            <button
              type="button"
              className="v2-tab"
              aria-pressed={isV1Tab}
              onClick={() => switchTab("v1")}
            >
              工作区登录
            </button>
            <button
              type="button"
              className="v2-tab"
              aria-pressed={!isV1Tab}
              onClick={() => switchTab("v2")}
            >
              账户登录
            </button>
          </div>

          <div
            className="auth-mode-content"
            key={isV1Tab ? "v1" : `v2-${v2Stage}`}
          >
            {isV1Tab ? (
              <>
                <h2 className="auth-card-title">欢迎回来</h2>
                <p className="auth-card-sub">使用用户名或邮箱登录你的工作台。</p>
                <form className="auth-form" onSubmit={handleV1Login} noValidate>
                  <div className="form-alert" role="alert" hidden={!v1Alert}>
                    {v1Alert}
                  </div>
                  <FormField label="用户名或邮箱" error={v1Errors.login}>
                    <input
                      className="field-input"
                      name="login"
                      autoComplete="username"
                      placeholder="username 或 name@example.com"
                      value={v1Fields.login}
                      onChange={(event) => setV1Field("login", event.target.value)}
                    />
                  </FormField>
                  <FormField label="密码" error={v1Errors.password}>
                    <input
                      className="field-input"
                      name="password"
                      type="password"
                      autoComplete="current-password"
                      placeholder="请输入密码"
                      value={v1Fields.password}
                      onChange={(event) => setV1Field("password", event.target.value)}
                    />
                  </FormField>
                  <div className="auth-form-footer">
                    <button
                      type="submit"
                      className="btn btn-primary btn-block"
                      disabled={v1Submitting}
                    >
                      {v1Submitting ? "登录中…" : "登录"}
                    </button>
                  </div>
                </form>
              </>
            ) : v2Stage === "challenge" ? (
              <>
                <h2 className="auth-card-title">两步验证</h2>
                <p className="auth-card-sub">输入认证器 App 生成的 6-8 位动态验证码。</p>
                <form className="auth-form" onSubmit={handleV2Mfa} noValidate>
                  <div className="form-alert" role="alert" hidden={!v2Alert}>
                    {v2Alert}
                  </div>
                  <div className="field">
                    <span className="field-label">动态验证码</span>
                    <OtpInput value={otp} onChange={handleOtpChange} />
                  </div>
                  <div className="auth-form-footer">
                    <button
                      type="submit"
                      className="btn btn-primary btn-block"
                      disabled={v2Submitting || retryAfter > 0 || otp.length < 6}
                    >
                      {v2Submitting ? "验证中…" : "验证并登录"}
                    </button>
                    <RetryHint retryAfter={retryAfter} />
                    <p className="auth-switch-hint">
                      <button
                        type="button"
                        className="auth-switch-link"
                        onClick={resetV2Challenge}
                      >
                        返回重新登录
                      </button>
                    </p>
                  </div>
                </form>
              </>
            ) : (
              <>
                <h2 className="auth-card-title">账户登录</h2>
                <p className="auth-card-sub">使用你的 AgentCraft 账户（邮箱）登录。</p>
                <form className="auth-form" onSubmit={handleV2Login} noValidate>
                  <div className="form-alert" role="alert" hidden={!v2Alert}>
                    {v2Alert}
                  </div>
                  <FormField label="邮箱" error={v2Errors.email}>
                    <input
                      className="field-input"
                      name="email"
                      type="email"
                      autoComplete="email"
                      placeholder="name@example.com"
                      value={v2Fields.email}
                      onChange={(event) => setV2Field("email", event.target.value)}
                    />
                  </FormField>
                  <FormField label="密码" error={v2Errors.password}>
                    <input
                      className="field-input"
                      name="password"
                      type="password"
                      autoComplete="current-password"
                      placeholder="请输入密码"
                      value={v2Fields.password}
                      onChange={(event) => setV2Field("password", event.target.value)}
                    />
                  </FormField>
                  <div className="auth-form-footer">
                    <button
                      type="submit"
                      className="btn btn-primary btn-block"
                      disabled={v2Submitting || retryAfter > 0}
                    >
                      {v2Submitting ? "登录中…" : "登录"}
                    </button>
                    <RetryHint retryAfter={retryAfter} />
                    {/* 忘记密码走 V2 重置流（FE-T5）；仅 V2 Tab——V1 工作区账户
                        为独立双库账户，无重置端点，放 V1 Tab 会误导 */}
                    <p className="auth-switch-hint">
                      <Link className="auth-switch-link" to="/password-reset">
                        忘记密码？
                      </Link>
                    </p>
                  </div>
                </form>
              </>
            )}
          </div>
        </div>
      </section>
    </main>
  );
}
