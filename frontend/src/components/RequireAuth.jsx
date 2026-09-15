import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext.jsx";

/**
 * 路由守卫（双轨化 E12；专家判据 T11 汇流）：
 * - authReady = v1Ready && v2Ready（双轨启动探测落定）之前渲染 null；
 * - 非专家路由：V1/V2 任一会话存在即放行（v1User || v2User），两者皆无才
 *   Navigate /login 并记录来源；
 * - requireExpert 路由消费上下文汇流值 isExpert（双判据：V2 entitlements 含
 *   expert_author 或 V1 role==="expert"；T14 收敛单判据）——V2-only
 *   expert_author 用户自此可入 /my-experts 等专家页。
 */
export default function RequireAuth({ requireExpert = false, children }) {
  const { authReady, user, v2User, isExpert } = useAuth();
  const location = useLocation();

  if (!authReady) {
    return null;
  }
  if (!user && !v2User) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }
  if (requireExpert && !isExpert) {
    return <Navigate to="/" replace />;
  }
  return children;
}
