import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import {
  V2_SESSION_EXPIRED_EVENT,
  newIdempotencyKey,
  requestV2,
  setCsrfToken,
} from "../api/v2/client.js";
import { V2_AUTH, V2_USERS } from "../api/v2/routes.js";

const AuthContext = createContext(null);

/**
 * V2 会话过期 401 时暂存来源路径的 sessionStorage 键（D13）；恢复由 LoginPage
 * 的 V2 登录成功路径承接（消费即清除）。
 */
export const V2_LOGIN_FROM_STORAGE_KEY = "agentcraft_v2_login_from";

/**
 * from 白名单（安全审查 I-4）：仅站内根相对路径——`/` 开头且非 `//` 开头
 * （`//evil.com` 是协议相对 URL，恢复时会被解析为跨源导航）。写侧（401 handler
 * 暂存 location.pathname）与读侧（LoginPage 恢复）双侧过滤；读侧防御存储被
 * 篡改或历史遗留脏值。
 */
export function isSafeInternalPath(value) {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//");
}

function stashLoginFrom(pathname) {
  if (!isSafeInternalPath(pathname)) {
    return;
  }
  try {
    window.sessionStorage.setItem(V2_LOGIN_FROM_STORAGE_KEY, pathname);
  } catch {
    // sessionStorage 不可用（隐私模式等）→ 退化为无 from 恢复（登录后回落 /）
  }
}

const SNAKE_KEY_PATTERN = /_([a-z0-9])/g;

function toCamelKey(key) {
  return key.replace(SNAKE_KEY_PATTERN, (_, ch) => ch.toUpperCase());
}

/**
 * V2 user 形状归一：snake→camel（loginV2 / loginV2Mfa / 启动探测共用，
 * 调用方统一消费 camel 键）。非对象输入归一为 null。
 */
function mapV2User(raw) {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  return Object.fromEntries(
    Object.entries(raw).map(([key, value]) => [toCamelKey(key), value])
  );
}

/**
 * V2 登录成功信封 {user, csrf_token}（backend/v2/login_service.py _login_body）：
 * 捕获 csrf_token 到客户端内存态、归一并置入上下文。
 */
function captureV2LoginSuccess(result, onUser) {
  if (result.data?.csrf_token) {
    setCsrfToken(result.data.csrf_token);
  }
  const user = mapV2User(result.data?.user);
  onUser(user);
  return { user };
}

/**
 * 认证上下文（Phase 8 T14 会话归一）：V1 轨已随 cutover 删除（D2），唯一
 * 会话域 = V2 cookie 会话——启动 silent 探测 /api/users/me（401=未登录，
 * 其它失败 catch-all 降级未登录），loginV2/loginV2Mfa/logoutV2 与
 * v2:session-expired 事件订阅；`authReady` 即 V2 探测落定位。
 */
export function AuthProvider({ children }) {
  const [v2User, setV2User] = useState(null);
  const [v2Ready, setV2Ready] = useState(false);

  // 挂载时一次性清理 V1 遗留 token（安全审查 I-5：cutover 后 v1Token 残留
  // 清零——V1 面（api/client.js）已删，无人再读该键；顺手清掉历史脏值）
  useEffect(() => {
    localStorage.removeItem("agentcraft_token");
  }, []);

  // V2 启动探测：silent 401 = 未登录（不派发事件）；500/网络错误 catch-all
  // 同样置 ready（降级未登录 + 控制台告警），保证 authReady 落定、不白屏
  useEffect(() => {
    let cancelled = false;
    async function probeV2Session() {
      try {
        const result = await requestV2(`${V2_USERS}/me`, { silent: true });
        if (!cancelled) {
          setV2User(mapV2User(result.data));
        }
      } catch (error) {
        if (cancelled) {
          return;
        }
        if (error?.status !== 401) {
          console.warn("[auth] V2 会话探测失败，按未登录降级：", error);
        }
        setV2User(null);
      } finally {
        if (!cancelled) {
          setV2Ready(true);
        }
      }
    }
    probeV2Session();
    return () => {
      cancelled = true;
    };
  }, []);

  // V2 会话过期事件（requestV2 非 silent 401 SESSION_EXPIRED 派发）：
  // 先 pathname 守卫防 /login 同页重载循环，再清态 + 暂存 from + 跳转（绕开
  // router 依赖）。D13/I-4：全页跳转丢 router state，改 sessionStorage 暂存
  // 来源路径（白名单见 isSafeInternalPath），恢复由 LoginPage 登录成功后的
  // navigate() 承接——恢复路径一律 react-router navigate，禁 location.assign。
  useEffect(() => {
    function handleSessionExpired() {
      if (window.location.pathname === "/login") {
        return;
      }
      setV2User(null);
      stashLoginFrom(window.location.pathname);
      window.location.assign("/login?v2=1");
    }
    window.addEventListener(V2_SESSION_EXPIRED_EVENT, handleSessionExpired);
    return () => {
      window.removeEventListener(V2_SESSION_EXPIRED_EVENT, handleSessionExpired);
    };
  }, []);

  /**
   * 登录成功后的静默 users/me 刷新（T11 移交 M1，T14 必办）：登录信封
   * （_login_body）不含 entitlements，专家面点亮依赖刷新补全。探测失败
   * （401/网络）静默收敛——保持登录信封用户，不清会话（与 refreshV2User
   * 的「失败即登出」语义区分）。
   */
  const refreshAfterLogin = useCallback(async () => {
    try {
      const probe = await requestV2(`${V2_USERS}/me`, { silent: true });
      const refreshed = mapV2User(probe.data);
      if (refreshed) {
        setV2User(refreshed);
      }
    } catch {
      // 静默：信封用户已置位，entitlements 由下次启动探测/refreshV2User 补全
    }
  }, []);

  /** V2 登录：成功返回 {user}（snake→camel）；TOTP 用户返回 {mfaRequired, challengeId} */
  const loginV2 = useCallback(
    async (email, password) => {
      const result = await requestV2(`${V2_AUTH}/login`, {
        method: "POST",
        body: { email, password },
      });
      if (result.data?.mfa_required) {
        // 调用方不见 mfa_required / mfa_challenge_id 原始键
        return { mfaRequired: true, challengeId: result.data.mfa_challenge_id };
      }
      const outcome = captureV2LoginSuccess(result, setV2User);
      // M1：登录信封无 entitlements——静默刷新补全专家判据
      await refreshAfterLogin();
      return outcome;
    },
    [refreshAfterLogin]
  );

  /** V2 MFA 挑战验证：成功建会话，返回 {user}（snake→camel + csrf 捕获） */
  const loginV2Mfa = useCallback(
    async (challengeId, totpCode) => {
      const result = await requestV2(`${V2_AUTH}/login/mfa`, {
        method: "POST",
        body: { mfa_challenge_id: challengeId, totp_code: totpCode },
      });
      const outcome = captureV2LoginSuccess(result, setV2User);
      await refreshAfterLogin();
      return outcome;
    },
    [refreshAfterLogin]
  );

  /**
   * V2 登出：200 与 401 SESSION_EXPIRED 均等价收敛为本地登出——401 时 requestV2
   * 已清 csrf 并派发事件（跳转由事件 handler 收口），此处捕获后正常 resolve，
   * 不得向调用方或用户暴露错误、不二次跳转。
   */
  const logoutV2 = useCallback(async () => {
    try {
      await requestV2(`${V2_AUTH}/logout`, { method: "POST" });
      setCsrfToken(null);
    } catch {
      // 非 401 失败（网络等）会话可能仍有效，不动 csrf；一律收敛为本地登出
    } finally {
      setV2User(null);
    }
  }, []);

  /**
   * V2 邀请接受（FE-T4）：提交 accept（幂等键内部生成，义务端点）→ 存 csrf
   * （响应体 data.csrf_token）→ silent 探测 users/me 确认会话落定（重放请求
   * 无 Set-Cookie 的陷阱靠探测识别）。探测 200 → 置位 v2User（pending，pending
   * 横幅依赖）并返回 {sessionConfirmed: true}；探测任何失败（401 重放/竞态、
   * 网络）→ 返回 {sessionConfirmed: false}——调用方渲染引导文案但不得渲染任何
   * 调用认证端点的按钮。accept 本身失败（409/429 等）原样抛 V2ApiError 由页面分流。
   */
  const acceptInvitation = useCallback(async (email, password, invitationToken) => {
    const result = await requestV2(`${V2_AUTH}/invitations/accept`, {
      method: "POST",
      body: { invitation_token: invitationToken, email, password },
      idempotencyKey: newIdempotencyKey(),
    });
    if (result.data?.csrf_token) {
      setCsrfToken(result.data.csrf_token);
    }
    try {
      const probe = await requestV2(`${V2_USERS}/me`, { silent: true });
      const user = mapV2User(probe.data);
      setV2User(user);
      return { sessionConfirmed: Boolean(user) };
    } catch {
      return { sessionConfirmed: false };
    }
  }, []);

  /**
   * V2 本地会话态清理（FE-T7）：清内存 csrf + 置空 v2User。
   * 零网络请求、零事件派发——注销受理（DangerZone）与撤销本机会话
   * （SessionsCard）后的静默收尾：不显式清会让陈旧 csrf/死 cookie 在后续
   * 请求触发 401 事件，打断「注销中」/登录跳转。
   */
  const clearV2Session = useCallback(() => {
    setCsrfToken(null);
    setV2User(null);
  }, []);

  /**
   * V2 会话刷新（FE-T4）：silent 探测 users/me 并置位/清除 v2User。
   * 邮箱验证成功后 pending→active 的状态翻转依赖此刷新；探测失败（401 会话
   * 失效、网络异常）与启动探测 catch-all 同语义——降级未登录。
   */
  const refreshV2User = useCallback(async () => {
    try {
      const probe = await requestV2(`${V2_USERS}/me`, { silent: true });
      const user = mapV2User(probe.data);
      setV2User(user);
      return user;
    } catch {
      setV2User(null);
      return null;
    }
  }, []);

  const value = useMemo(
    () => ({
      v2User,
      v2Ready,
      authReady: v2Ready, // T14 会话归一：唯一会话域，探测落定即可渲染守卫路由
      // isExpert 单判据（T11 注释钉兑现）：V2 entitlements 含 expert_author。
      // 登录信封无 entitlements——loginV2/loginV2Mfa 成功路径已静默 users/me
      // 刷新补全（refreshAfterLogin），刚登录的 V2 专家立即点亮专家面。
      isExpert: Boolean(v2User?.entitlements?.includes("expert_author")),
      loginV2,
      loginV2Mfa,
      logoutV2,
      clearV2Session,
      acceptInvitation,
      refreshV2User,
    }),
    [
      v2User,
      v2Ready,
      loginV2,
      loginV2Mfa,
      logoutV2,
      clearV2Session,
      acceptInvitation,
      refreshV2User,
    ]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth 必须在 AuthProvider 内使用");
  }
  return context;
}
