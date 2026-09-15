import { NavLink, Outlet } from "react-router-dom";
import { Users, UserCircle, SealCheck, Flag, Sliders, Scroll } from "@phosphor-icons/react";

/** 子导航项：T12a 交付 invitations/users；reviews/reports/catalog/audit 由 T12b 落地 */
const ADMIN_NAV_ITEMS = [
  { to: "/admin/invitations", label: "邀请管理", Icon: UserCircle },
  { to: "/admin/users", label: "用户管理", Icon: Users },
  { to: "/admin/reviews", label: "审核队列", Icon: SealCheck },
  { to: "/admin/reports", label: "举报队列", Icon: Flag },
  { to: "/admin/catalog", label: "目录管理", Icon: Sliders },
  { to: "/admin/audit", label: "审计查询", Icon: Scroll },
];

/**
 * admin 控制台壳层（Phase 8 T12a）：/admin 子导航 + Outlet。
 * 守卫 RequireAdmin 挂在 router.jsx 路由元素上（gate API 供页面消费）；
 * 治理四页（T12b）落位后本壳子导航即全部点亮。
 */
export default function AdminLayout() {
  return (
    <main className="app-main">
      <div className="page-header">
        <h1 className="page-title">管理控制台</h1>
        <p className="page-sub">操作全量审计留痕；写操作需填写原因并经二次确认。</p>
      </div>
      <nav className="manage-toolbar" aria-label="管理控制台导航">
        {ADMIN_NAV_ITEMS.map(({ to, label, Icon }) => (
          <NavLink key={to} to={to} className="v2-tab" aria-label={label}>
            <Icon size={14} aria-hidden="true" />
            {label}
          </NavLink>
        ))}
      </nav>
      <Outlet />
    </main>
  );
}
