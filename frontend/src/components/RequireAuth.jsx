import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext.jsx";

/**
 * 路由守卫：未登录跳转 /login 并记录来源；requireExpert 时非专家用户回首页。
 */
export default function RequireAuth({ requireExpert = false, children }) {
  const { isReady, isAuthenticated, isExpert } = useAuth();
  const location = useLocation();

  if (!isReady) {
    return null;
  }
  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  if (requireExpert && !isExpert) {
    return <Navigate to="/" replace />;
  }
  return children;
}
