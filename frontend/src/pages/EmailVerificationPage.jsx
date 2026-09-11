import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { newIdempotencyKey, requestV2 } from "../api/v2/client.js";
import { V2_AUTH } from "../api/v2/routes.js";
import { useAuth } from "../auth/AuthContext.jsx";
import AuthBrandPanel from "../components/AuthBrandPanel.jsx";

/**
 * 邮箱验证落地页（FE-T4）：
 * - 令牌驱动公开流（E11）：不挂守卫——pending 用户会话有效、接受邀请即种会话，
 *   挂 guest 门会令验证链接永远弹回、pending 永不转 active；confirm 为公开令牌
 *   端点（无会话无 CSRF）。独立于应用壳（P01 全屏构图）。
 * - 挂载即自动提交 confirm，无论当前 V1/V2 会话状态。
 * - React.StrictMode 双执行陷阱：幂等键在 useRef 初始化器生成 + submitted 闸门
 *   ——effect 体内生成会产生两把 key → 两发请求，成功页闪「失效」态。effect
 *   不设 cancelled 收口：闸门保证仅一发请求，首发的 setState 正常落定。
 * - 200 → 成功页（已登录附「返回工作台」，否则「前往登录」）；确认成功后统一
 *   refresh 探测置位最新状态（pending→active，pending 横幅随之消失；匿名 401
 *   维持未登录）。400 EMAIL_NOT_VERIFIED → 失效页；其它失败 → 失败页（重试
 *   复用同一幂等键）。
 */

export default function EmailVerificationPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { v2User, refreshV2User } = useAuth();

  // 态机：verifying（自动提交中）→ success | invalid | failed
  const [stage, setStage] = useState("verifying");
  const [failureMessage, setFailureMessage] = useState("");

  const token = searchParams.get("token") || "";
  // useRef 初始化器： StrictMode 双渲染下仅首把键保留（生成两次、使用一次）
  const idempotencyKeyRef = useRef(newIdempotencyKey());
  // submitted 闸门：StrictMode effect 双跑仅放行首次
  const autoSubmittedRef = useRef(false);

  /** confirm 提交（幂等义务端点）；手动重试复用同一把幂等键 */
  async function runConfirm() {
    try {
      await requestV2(`${V2_AUTH}/email-verification/confirm`, {
        method: "POST",
        body: { token },
        idempotencyKey: idempotencyKeyRef.current,
      });
      setStage("success");
      // 统一刷新会话状态：pending 会话 → 探测置位 active（横幅随之消失）；
      // 无会话（匿名）→ silent 401 → 维持未登录。挂载时 v2User 尚未落定
      // （启动探测与 confirm 并行），按闭包判断会漏刷新，故不设前置门。
      refreshV2User();
    } catch (error) {
      if (error.code === "EMAIL_NOT_VERIFIED") {
        setStage("invalid");
        return;
      }
      setFailureMessage(error.message || "请求失败，请稍后重试");
      setStage("failed");
    }
  }

  useEffect(() => {
    if (autoSubmittedRef.current) {
      return;
    }
    autoSubmittedRef.current = true;
    runConfirm();
    // 挂载即自动提交：令牌来自 URL，仅依赖挂载时上下文（eslint-disable 对齐无 lint 仓）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (stage === "verifying") {
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }}>
            <h2 className="auth-card-title">正在验证邮箱…</h2>
            <p className="auth-card-sub">正在确认你的验证链接，请稍候。</p>
          </div>
        </section>
      </main>
    );
  }

  if (stage === "invalid") {
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }} role="alert">
            <h2 className="auth-card-title">验证链接无效或已失效</h2>
            <p className="auth-card-sub">
              该验证链接不存在或已过期。请重新获取验证邮件，或登录后在顶部横幅中重发。
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

  if (stage === "failed") {
    return (
      <main className="auth-page">
        <AuthBrandPanel />
        <section className="auth-panel">
          <div className="auth-card rise" style={{ "--rise-index": 1 }}>
            <h2 className="auth-card-title">验证失败</h2>
            <div className="form-alert" role="alert">
              {failureMessage}
            </div>
            <div className="auth-form-footer">
              <button type="button" className="btn btn-primary btn-block" onClick={runConfirm}>
                重新尝试
              </button>
              <p className="auth-switch-hint">
                <Link className="auth-switch-link" to="/login?v2=1">
                  前往登录
                </Link>
              </p>
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
          <h2 className="auth-card-title">邮箱验证成功</h2>
          <p className="auth-card-sub">你的邮箱已完成验证，账户已就绪。</p>
          <div className="auth-form-footer">
            {v2User ? (
              <button
                type="button"
                className="btn btn-primary btn-block"
                onClick={() => navigate("/")}
              >
                返回工作台
              </button>
            ) : (
              <Link className="btn btn-primary btn-block" to="/login?v2=1">
                前往登录
              </Link>
            )}
          </div>
        </div>
      </section>
    </main>
  );
}
