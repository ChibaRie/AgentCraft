import { useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { newIdempotencyKey, requestV2, setCsrfToken } from "../api/v2/client.js";
import { V2_ACCOUNT } from "../api/v2/routes.js";
import { useAuth } from "../auth/AuthContext.jsx";
import AuthBrandPanel from "../components/AuthBrandPanel.jsx";
import FormField from "../components/FormField.jsx";
import { useRetryAfter } from "../hooks/useRetryAfter.js";

/**
 * 注销撤销落地页（FE-T5，冷却邮件 ?token= 直达）：
 * - 公开令牌流（E11）：不挂守卫，独立于应用壳（P01 全屏构图，同 /login）。
 *   用户注销后全会话已失效（deleting 401），必然匿名到达——挂任何门都会弹回。
 * - 说明文案 + 登录密码确认 → `POST /account/deletion/cancel`（公开端点，无会话
 *   无 CSRF；幂等义务端点 A5）。幂等键 useRef 初始化器（StrictMode 双渲染仅首把
 *   键保留）+ in-flight 提交闸门（T4 同款陷阱收口）；失败重试复用同一把键。
 * - 200 双 cookie 新会话：csrf_token 入内存态 → silent 探测 users/me 确认会话
 *   落定（await 后再导航，防守卫在 v2User 置位前弹回）→ 回工作台。
 * - 409 ACCOUNT_DELETING「撤销链接无效或已过期」→ 失效页（后端全部失效形态统一
 *   此码此文案，防探测）；401 验密失败 → 内联 alert 留在表单；429 → 倒计时。
 */

export default function DeletionCancelPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { refreshV2User } = useAuth();
  const { retryAfter, start: startRetryAfter } = useRetryAfter();

  const cancelToken = searchParams.get("token") || "";
  // 态机：form（表单）→ invalid（409 失效态）；200 不落中间态（探测后直接回工作台）
  const [stage, setStage] = useState(() => (cancelToken ? "form" : "invalid"));
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [alert, setAlert] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // useRef 初始化器：StrictMode 双渲染下仅首把键保留（生成两次、使用一次）；
  // 失败重试复用同一把键
  const idempotencyKeyRef = useRef(newIdempotencyKey());
  // 提交闸门：in-flight 期间忽略重入提交（双击/竞态）
  const inFlightRef = useRef(false);

  function setField(value) {
    setPassword(value);
    setError("");
    setAlert("");
  }

  async function runCancel() {
    if (inFlightRef.current) {
      return;
    }
    inFlightRef.current = true;
    setSubmitting(true);
    setAlert("");
    try {
      const result = await requestV2(`${V2_ACCOUNT}/deletion/cancel`, {
        method: "POST",
        body: { cancel_token: cancelToken, password },
        idempotencyKey: idempotencyKeyRef.current,
      });
      if (result.data?.csrf_token) {
        setCsrfToken(result.data.csrf_token);
      }
      // silent 探测确认新会话落定（重放响应无 Set-Cookie 的陷阱靠探测识别）；
      // await 后再导航：守卫消费 v2User，探测未落定就导航会被弹回 /login
      await refreshV2User();
      navigate("/", { replace: true });
    } catch (caught) {
      if (caught.status === 409 && caught.code === "ACCOUNT_DELETING") {
        setStage("invalid");
        return;
      }
      if (caught.status === 429) {
        startRetryAfter(caught.retryAfter);
        setAlert(caught.message || "请求过于频繁，请稍后重试");
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
    if (!password) {
      setError("请输入登录密码");
      return;
    }
    if (!cancelToken) {
      return;
    }
    runCancel();
  }

  if (stage === "invalid") {
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }} role="alert">
            <h2 className="auth-card-title">撤销链接无效或已过期</h2>
            <p className="auth-card-sub">
              该撤销链接不存在、已被使用或已过期。如账户仍处于注销流程中，请通过邮件中的最新链接操作。
            </p>
            <p className="auth-switch-hint">
              <Link className="auth-switch-link" to="/login?v2=1">
                前往登录
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
          <h2 className="auth-card-title">撤销账户注销</h2>
          <p className="auth-card-sub">
            你的账户已申请注销，正在等待期倒计时，到期后将被永久删除且不可恢复。在此期间可验证身份后撤销注销，恢复账户的正常使用。
          </p>
          <form className="auth-form" onSubmit={handleSubmit} noValidate>
            <div className="form-alert" role="alert" hidden={!alert}>
              {alert}
            </div>
            <FormField label="登录密码" error={error}>
              <input
                className="field-input"
                name="password"
                type="password"
                autoComplete="current-password"
                placeholder="请输入登录密码"
                value={password}
                onChange={(event) => setField(event.target.value)}
              />
            </FormField>
            <div className="auth-form-footer">
              <button
                type="submit"
                className="btn btn-primary btn-block"
                disabled={submitting || retryAfter > 0}
              >
                {submitting ? "提交中…" : "撤销注销并恢复账户"}
              </button>
              {retryAfter > 0 ? (
                <p className="v2-retry-hint" role="status">
                  操作过于频繁，请等待 {retryAfter} 秒后再试
                </p>
              ) : null}
            </div>
          </form>
        </div>
      </section>
    </main>
  );
}
