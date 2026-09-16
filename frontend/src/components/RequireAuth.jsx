import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext.jsx";

/**
 * 路由守卫（Phase 8 T14 会话归一：唯一会话域 = V2 cookie 会话）：
 * - authReady（V2 启动探测落定）之前渲染 null；
 * - 无 V2 会话 → Navigate /login 并记录来源（RequireGuest 删除背景下的
 *   循环防护沿既有 pathname≠/login 语义——/login 页本身不挂本守卫）；
 * - requireExpert 路由消费上下文单判据 isExpert（V2 entitlements 含
 *   expert_author；T14 收敛，V1 role 分支已随 V1 轨删除）。
 */
export default function RequireAuth({ requireExpert = false, children }) {
  const { authReady, v2User, isExpert } = useAuth();
  const location = useLocation();

  if (!authReady) {
    return null;
  }
  if (!v2User) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  if (requireExpert && !isExpert) {
    return <Navigate to="/" replace />;
  }
  return children;
}
