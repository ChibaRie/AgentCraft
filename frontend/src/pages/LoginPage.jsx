import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Check, Eye, Globe, UploadSimple } from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";

const USERNAME_MIN = 2;
const USERNAME_MAX = 30;
const PASSWORD_MIN = 6;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const BRAND_POINTS = [
  { icon: Globe, label: "发布专家，沉淀可复用的人设与方法论" },
  { icon: UploadSimple, label: "召唤专家，挂载项目目录与任务文件" },
  { icon: Check, label: "Skill 注入上下文，任务能力随建随用" },
];

function validateUsername(value) {
  const trimmed = value.trim();
  if (trimmed.length < USERNAME_MIN || trimmed.length > USERNAME_MAX) {
    return `用户名需为 ${USERNAME_MIN}-${USERNAME_MAX} 个字符`;
  }
  return "";
}

function validateEmail(value) {
  const trimmed = value.trim();
  if (!EMAIL_PATTERN.test(trimmed)) {
    return "邮箱格式不正确";
  }
  return "";
}

function validatePassword(value) {
  if (value.length < PASSWORD_MIN) {
    return "密码至少 6 位";
  }
  return "";
}

function Field({ label, error, children }) {
  return (
    <div className="field">
      <label className="field-label">
        {label}
        {children}
      </label>
      <div className="field-error" role="alert">
        {error}
      </div>
    </div>
  );
}

export default function LoginPage() {
  const { user, login, register } = useAuth();
  const [mode, setMode] = useState("login");
  const [fields, setFields] = useState({
    login: "",
    password: "",
    username: "",
    email: "",
    confirmPassword: "",
  });
  const [errors, setErrors] = useState({});
  const [formAlert, setFormAlert] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  if (user) {
    return <Navigate to={location.state?.from || "/"} replace />;
  }

  function setField(name, value) {
    setFields((current) => ({ ...current, [name]: value }));
    setErrors((current) => ({ ...current, [name]: "" }));
    setFormAlert("");
  }

  function switchMode(nextMode) {
    // 切换登录/注册时清空两种表单的瞬时错误，避免串场
    setMode(nextMode);
    setErrors({});
    setFormAlert("");
  }

  function applyServerError(error) {
    if (error.code === "USERNAME_EXISTS" || error.code === "EMAIL_EXISTS") {
      // 服务端消息已含 PRD 提示文案（"用户名 [x] 已被使用" / "邮箱 [x] 已被注册"）
      const field = error.code === "USERNAME_EXISTS" ? "username" : "email";
      setErrors((current) => ({ ...current, [field]: error.message }));
      return;
    }
    setFormAlert(error.message || "请求失败，请稍后重试");
  }

  async function handleLogin(event) {
    event.preventDefault();
    const nextErrors = {
      login: fields.login.trim() ? "" : "请输入用户名或邮箱",
      password: fields.password ? "" : "请输入密码",
    };
    setErrors(nextErrors);
    if (nextErrors.login || nextErrors.password) {
      return;
    }
    setIsSubmitting(true);
    try {
      await login(fields.login, fields.password);
      navigate(location.state?.from || "/", { replace: true });
    } catch (error) {
      applyServerError(error);
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleRegister(event) {
    event.preventDefault();
    const nextErrors = {
      username: validateUsername(fields.username),
      email: validateEmail(fields.email),
      password: validatePassword(fields.password),
      confirmPassword:
        fields.confirmPassword === fields.password ? "" : "两次输入的密码不一致",
    };
    setErrors(nextErrors);
    if (Object.values(nextErrors).some(Boolean)) {
      return;
    }
    setIsSubmitting(true);
    try {
      await register(fields.username.trim(), fields.email.trim(), fields.password);
      navigate(location.state?.from || "/", { replace: true });
    } catch (error) {
      applyServerError(error);
    } finally {
      setIsSubmitting(false);
    }
  }

  const isLogin = mode === "login";

  return (
    <main className="auth-page">
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

      <section className="auth-panel">
        <div className="auth-card rise" style={{ "--rise-index": 1 }}>
          <div className="auth-tabs" data-mode={mode} role="group" aria-label="登录或注册">
            <span className="auth-tabs-thumb" aria-hidden="true" />
            <button
              type="button"
              className="auth-tab"
              aria-pressed={isLogin}
              onClick={() => switchMode("login")}
            >
              登录
            </button>
            <button
              type="button"
              className="auth-tab"
              aria-pressed={!isLogin}
              onClick={() => switchMode("register")}
            >
              注册
            </button>
          </div>

          <div className="auth-mode-content" key={mode}>
            {isLogin ? (
              <>
                <h2 className="auth-card-title">欢迎回来</h2>
                <p className="auth-card-sub">使用用户名或邮箱登录你的工作台。</p>
                <form className="auth-form" onSubmit={handleLogin} noValidate>
                  <div className="form-alert" role="alert" hidden={!formAlert}>
                    {formAlert}
                  </div>
                  <Field label="用户名或邮箱" error={errors.login}>
                    <input
                      className="field-input"
                      name="login"
                      autoComplete="username"
                      placeholder="username 或 name@example.com"
                      value={fields.login}
                      onChange={(event) => setField("login", event.target.value)}
                    />
                  </Field>
                  <Field label="密码" error={errors.password}>
                    <input
                      className="field-input"
                      name="password"
                      type="password"
                      autoComplete="current-password"
                      placeholder="请输入密码"
                      value={fields.password}
                      onChange={(event) => setField("password", event.target.value)}
                    />
                  </Field>
                  <div className="auth-form-footer">
                    <button
                      type="submit"
                      className="btn btn-primary btn-block"
                      disabled={isSubmitting}
                    >
                      {isSubmitting ? "登录中…" : "登录"}
                    </button>
                    <p className="auth-switch-hint">
                      还没有账号？
                      <button
                        type="button"
                        className="auth-switch-link"
                        onClick={() => switchMode("register")}
                      >
                        创建账号
                      </button>
                    </p>
                  </div>
                </form>
              </>
            ) : (
              <>
                <h2 className="auth-card-title">创建你的工作台</h2>
                <p className="auth-card-sub">注册即可浏览与召唤专家，随时可申请成为专家用户。</p>
                <form className="auth-form" onSubmit={handleRegister} noValidate>
                  <div className="form-alert" role="alert" hidden={!formAlert}>
                    {formAlert}
                  </div>
                  <Field label="用户名" error={errors.username}>
                    <input
                      className="field-input"
                      name="username"
                      autoComplete="username"
                      placeholder="2-30 个字符"
                      value={fields.username}
                      onChange={(event) => setField("username", event.target.value)}
                    />
                  </Field>
                  <Field label="邮箱" error={errors.email}>
                    <input
                      className="field-input"
                      name="email"
                      type="email"
                      autoComplete="email"
                      placeholder="name@example.com"
                      value={fields.email}
                      onChange={(event) => setField("email", event.target.value)}
                    />
                  </Field>
                  <Field label="密码" error={errors.password}>
                    <input
                      className="field-input"
                      name="password"
                      type="password"
                      autoComplete="new-password"
                      placeholder="至少 6 位"
                      value={fields.password}
                      onChange={(event) => setField("password", event.target.value)}
                    />
                  </Field>
                  <Field label="确认密码" error={errors.confirmPassword}>
                    <input
                      className="field-input"
                      name="confirmPassword"
                      type="password"
                      autoComplete="new-password"
                      placeholder="请再次输入密码"
                      value={fields.confirmPassword}
                      onChange={(event) => setField("confirmPassword", event.target.value)}
                    />
                  </Field>
                  <div className="auth-form-footer">
                    <button
                      type="submit"
                      className="btn btn-primary btn-block"
                      disabled={isSubmitting}
                    >
                      {isSubmitting ? "注册中…" : "注册并登录"}
                    </button>
                    <p className="auth-switch-hint">
                      已有账号？
                      <button
                        type="button"
                        className="auth-switch-link"
                        onClick={() => switchMode("login")}
                      >
                        直接登录
                      </button>
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
