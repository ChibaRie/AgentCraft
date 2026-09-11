import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { getToken, request, setToken } from "../api/client.js";
import { V2_SESSION_EXPIRED_EVENT, requestV2, setCsrfToken } from "../api/v2/client.js";
import { V2_AUTH, V2_USERS } from "../api/v2/routes.js";

const AuthContext = createContext(null);

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
 * 双轨认证上下文（E1/E12）：
 * - V1 轨：Bearer token（localStorage）+ /api/users/me 启动恢复，逻辑原样保留；
 * - V2 轨：cookie 会话 + 启动 silent 探测 /api/v2/users/me（401=未登录，
 *   其它失败 catch-all 降级未登录），loginV2/loginV2Mfa/logoutV2 与
 *   v2:session-expired 事件订阅。
 * `isReady` 保留 V1 语义（V1 任务域页面继续消费）；路由守卫改消费
 * `authReady = v1Ready && v2Ready`（双探测落定）。
 */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [v1Ready, setV1Ready] = useState(false);
  const [v2User, setV2User] = useState(null);
  const [v2Ready, setV2Ready] = useState(false);

  const clearSession = useCallback(() => {
    setToken(null);
    setUser(null);
  }, []);

  // V1 轨恢复（原逻辑不动）：仅当 localStorage 有 token 才探测 /api/users/me
  useEffect(() => {
    let cancelled = false;
    async function restoreSession() {
      const tokenAtStart = getToken();
      if (!tokenAtStart) {
        setV1Ready(true);
        return;
      }
      try {
        const payload = await request("/api/users/me");
        if (!cancelled) {
          // 恢复期间若发生了 login/register/logout（token 已变化），丢弃过期结果
          if (getToken() === tokenAtStart) {
            setUser(payload.data);
          }
          setV1Ready(true);
        }
      } catch (error) {
        if (!cancelled) {
          if (error.status === 401 && getToken() === tokenAtStart) {
            clearSession();
          }
          setV1Ready(true);
        }
      }
    }
    restoreSession();
    return () => {
      cancelled = true;
    };
  }, [clearSession]);

  // V2 轨探测：silent 401 = 未登录（不派发事件）；500/网络错误 catch-all
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
  // 先 pathname 守卫防 /login 同页重载循环，再清态 + 跳转（绕开 router 依赖；
  // 丢失 state.from 为已知行为，E13）
  useEffect(() => {
    function handleSessionExpired() {
      if (window.location.pathname === "/login") {
        return;
      }
      setV2User(null);
      window.location.assign("/login?v2=1");
    }
    window.addEventListener(V2_SESSION_EXPIRED_EVENT, handleSessionExpired);
    return () => {
      window.removeEventListener(V2_SESSION_EXPIRED_EVENT, handleSessionExpired);
    };
  }, []);

  const login = useCallback(
    async (loginValue, password) => {
      const payload = await request("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ login: loginValue, password }),
      });
      setToken(payload.data.token);
      setUser(payload.data);
      return payload.data;
    },
    []
  );

  const register = useCallback(async (username, email, password) => {
    const payload = await request("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, email, password }),
    });
    setToken(payload.data.token);
    setUser(payload.data);
    return payload.data;
  }, []);

  const applyExpert = useCallback(async () => {
    const payload = await request("/api/users/me/expert", { method: "POST" });
    // 接口按规格只返回 {id,username,email,role}；合并保留 created_at 等本地已有字段
    setUser((current) => ({ ...current, ...payload.data }));
    return payload.data;
  }, []);

  const logout = useCallback(() => {
    clearSession();
  }, [clearSession]);

  /** V2 登录：成功返回 {user}（snake→camel）；TOTP 用户返回 {mfaRequired, challengeId} */
  const loginV2 = useCallback(async (email, password) => {
    const result = await requestV2(`${V2_AUTH}/login`, {
      method: "POST",
      body: { email, password },
    });
    if (result.data?.mfa_required) {
      // 调用方不见 mfa_required / mfa_challenge_id 原始键
      return { mfaRequired: true, challengeId: result.data.mfa_challenge_id };
    }
    return captureV2LoginSuccess(result, setV2User);
  }, []);

  /** V2 MFA 挑战验证：成功建会话，返回 {user}（snake→camel + csrf 捕获） */
  const loginV2Mfa = useCallback(async (challengeId, totpCode) => {
    const result = await requestV2(`${V2_AUTH}/login/mfa`, {
      method: "POST",
      body: { mfa_challenge_id: challengeId, totp_code: totpCode },
    });
    return captureV2LoginSuccess(result, setV2User);
  }, []);

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

  const value = useMemo(
    () => ({
      user,
      v1Ready,
      isReady: v1Ready, // V1 语义保留：MyExperts/SkillManage 等任务域页面继续消费
      v2User,
      v2Ready,
      authReady: v1Ready && v2Ready,
      isAuthenticated: Boolean(user),
      isExpert: user?.role === "expert",
      login,
      register,
      applyExpert,
      logout,
      loginV2,
      loginV2Mfa,
      logoutV2,
    }),
    [
      user,
      v1Ready,
      v2User,
      v2Ready,
      login,
      register,
      applyExpert,
      logout,
      loginV2,
      loginV2Mfa,
      logoutV2,
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
