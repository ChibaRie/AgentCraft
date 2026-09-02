import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { getToken, request, setToken } from "../api/client.js";

const AuthContext = createContext(null);

/**
 * 认证上下文：Token 持久化于 localStorage，用户信息挂载后从 /api/users/me 恢复。
 * 401（凭证失效）时清除本地会话。
 */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [isReady, setIsReady] = useState(false);

  const clearSession = useCallback(() => {
    setToken(null);
    setUser(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function restoreSession() {
      const tokenAtStart = getToken();
      if (!tokenAtStart) {
        setIsReady(true);
        return;
      }
      try {
        const payload = await request("/api/users/me");
        if (!cancelled) {
          // 恢复期间若发生了 login/register/logout（token 已变化），丢弃过期结果
          if (getToken() === tokenAtStart) {
            setUser(payload.data);
          }
          setIsReady(true);
        }
      } catch (error) {
        if (!cancelled) {
          if (error.status === 401 && getToken() === tokenAtStart) {
            clearSession();
          }
          setIsReady(true);
        }
      }
    }
    restoreSession();
    return () => {
      cancelled = true;
    };
  }, [clearSession]);

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

  const value = useMemo(
    () => ({
      user,
      isReady,
      isAuthenticated: Boolean(user),
      isExpert: user?.role === "expert",
      login,
      register,
      applyExpert,
      logout,
    }),
    [user, isReady, login, register, applyExpert, logout]
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
