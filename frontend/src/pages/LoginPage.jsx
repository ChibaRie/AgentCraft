import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Check, Globe, UploadSimple } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import OtpInput from "../components/OtpInput.jsx";

const BRAND_POINTS = [
  { icon: Globe, label: "发布专家，沉淀可复用的人设与方法论" },
  { icon: UploadSimple, label: "召唤专家，挂载项目目录与任务文件" },
  { icon: Check, label: "Skill 注入上下文，任务能力随建随用" },
];

function Field({ label, error, children }) {
  return (
    <div className="field">
      <label className="field-label">
        {label}
        {children}
      </label>
      {/* 仅在有错时渲染 role=alert：空告警对读屏器是噪音（原 :empty 仅靠 CSS 兜底） */}
      {error ? (
        <div className="field-error" role="alert">
          {error}
        </div>
      ) : null}
    </div>
  );
}

/** 品牌侧（V1/V2/提示态三态共用，纯展示） */
function BrandPanel() {
  return (
    <section className="auth-brand rise" aria-label="AgentCraft 产品介绍">
      <div className="auth-brand-eyebrow rise" style={{ "--rise-index": 0 }}>
        <span className="navbar-mark" aria-hidden="true" />
        AgentCraft
      </div>
      <div className="auth-brand-body rise" style={{ "--rise-index": 1 }}>
        <h1 className="auth-brand-title">
          把领域的经验，
          <br />
          交给一个可靠的专家。
        </h1>
        <p className="auth-brand-sub">
          AgentCraft 是运行在你本机的 AI 专家工作台：专家由你定义，Skill 与工具由你装配，
          任务在你授权的项目目录里完成。
        </p>
      </div>
      <div className="auth-brand-points rise" style={{ "--rise-index": 2 }}>
        {BRAND_POINTS.map((point) => (
          <div className="auth-brand-point" key={point.label}>
            <point.icon size={15} aria-hidden="true" />
            <span>
              <strong>{point.label.split("，")[0]}</strong>，{point.label.split("，")[1]}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

/** 429 Retry-After 倒计时（秒）：start(seconds) 置初值，每秒递减，归零自动解除禁用。 */
function useRetryAfter() {
  const [secondsLeft, setSecondsLeft] = useState(0);

  useEffect(() => {
    if (secondsLeft <= 0) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      setSecondsLeft((current) => Math.max(0, current - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [secondsLeft]);

  const start = useCallback((seconds) => {
    const parsed = Number(seconds);
    setSecondsLeft(Number.isFinite(parsed) && parsed > 0 ? Math.ceil(parsed) : 0);
  }, []);

  return { retryAfter: secondsLeft, start };
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
 * - URL ?v2=1（V2 会话过期 401 事件跳转落点）默认激活 V2 Tab。
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
        // 就地切换挑战卡片（不离开 /login）
        setChallengeId(result.challengeId);
        setOtp("");
        setV2Alert("");
        setV2Stage("challenge");
        return;
      }
      navigate(location.state?.from || "/", { replace: true });
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
      navigate(location.state?.from || "/", { replace: true });
    } catch (error) {
      applyV2Error(error);
    } finally {
      setV2Submitting(false);
    }
  }

  if (v2Stage === "blocked") {
    return (
      <main className="auth-page">
        <BrandPanel />
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
      <BrandPanel />

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
                  <Field label="用户名或邮箱" error={v1Errors.login}>
                    <input
                      className="field-input"
                      name="login"
                      autoComplete="username"
                      placeholder="username 或 name@example.com"
                      value={v1Fields.login}
                      onChange={(event) => setV1Field("login", event.target.value)}
                    />
                  </Field>
                  <Field label="密码" error={v1Errors.password}>
                    <input
                      className="field-input"
                      name="password"
                      type="password"
                      autoComplete="current-password"
                      placeholder="请输入密码"
                      value={v1Fields.password}
                      onChange={(event) => setV1Field("password", event.target.value)}
                    />
                  </Field>
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
                  <Field label="邮箱" error={v2Errors.email}>
                    <input
                      className="field-input"
                      name="email"
                      type="email"
                      autoComplete="email"
                      placeholder="name@example.com"
                      value={v2Fields.email}
                      onChange={(event) => setV2Field("email", event.target.value)}
                    />
                  </Field>
                  <Field label="密码" error={v2Errors.password}>
                    <input
                      className="field-input"
                      name="password"
                      type="password"
                      autoComplete="current-password"
                      placeholder="请输入密码"
                      value={v2Fields.password}
                      onChange={(event) => setV2Field("password", event.target.value)}
                    />
                  </Field>
                  <div className="auth-form-footer">
                    <button
                      type="submit"
                      className="btn btn-primary btn-block"
                      disabled={v2Submitting || retryAfter > 0}
                    >
                      {v2Submitting ? "登录中…" : "登录"}
                    </button>
                    <RetryHint retryAfter={retryAfter} />
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
