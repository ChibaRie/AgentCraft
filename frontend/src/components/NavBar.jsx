import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import {
  CaretDown,
  DoorOpen,
  Moon,
  Plug,
  Sun,
  UserCircle,
  Users,
  Wrench,
} from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
import { displayName } from "../auth/displayName.js";
import { V2_SESSION_EXPIRED_EVENT } from "../api/v2/client.js";
import { getEffectiveTheme, toggleTheme } from "../lib/theme.js";

/** 右上角日间/夜间切换：选择持久化（localStorage），未选择时跟随系统。 */
function ThemeToggle() {
  const [theme, setThemeState] = useState(getEffectiveTheme);

  function handleClick() {
    setThemeState(toggleTheme());
  }

  const isDark = theme === "dark";
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={handleClick}
      aria-label={isDark ? "切换到日间模式" : "切换到夜间模式"}
      title={isDark ? "切换到日间模式" : "切换到夜间模式"}
    >
      {isDark ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
    </button>
  );
}

/** 任务域入口（T11 移除 E12 置灰降级：任务面即将全 V2，V1 会话不再是
 *  可用性前提；无会话用户走普通登录引导）。 */
const TASK_DOMAIN_LINKS = [
  { to: "/discover", label: "专家中心" },
  { to: "/tasks", label: "任务" },
  { to: "/skills", label: "技能管理" },
];

function UserMenu() {
  const { v2User, isExpert, logoutV2 } = useAuth();
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!isOpen) {
      return undefined;
    }
    function handlePointerDown(event) {
      if (menuRef.current && !menuRef.current.contains(event.target)) {
        setIsOpen(false);
      }
    }
    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setIsOpen(false);
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen]);

  /**
   * 退出账户会话（FE-T7；logout 导航收敛规则）：
   * - 200 路径：logoutV2 已清 csrf + v2User → 本处理器跳 /login；
   * - 401 路径（死 cookie 重放）：requestV2 已派发会话过期事件，AuthProvider
   *   订阅已跳 /login?v2=1——本处理器不再二次跳转、不展示错误（等价收敛）。
   * 以「await 窗口内事件监听」区分两条路径：窗口内命中事件即让位给事件收口。
   */
  async function handleV2Logout() {
    setIsOpen(false);
    let sessionExpired = false;
    const markExpired = () => {
      sessionExpired = true;
    };
    window.addEventListener(V2_SESSION_EXPIRED_EVENT, markExpired, { once: true });
    try {
      await logoutV2();
    } finally {
      window.removeEventListener(V2_SESSION_EXPIRED_EVENT, markExpired);
    }
    if (!sessionExpired) {
      navigate("/login");
    }
  }

  // 渲染源 = V2 权威会话（T14 会话归一：唯一会话域）；无 username 时
  // displayName 取 email 前缀。专家徽标与菜单同源消费上下文单判据 isExpert。
  const name = displayName(v2User);

  return (
    <div className="usermenu" ref={menuRef}>
      <button
        type="button"
        className="usermenu-trigger"
        aria-haspopup="menu"
        aria-expanded={isOpen}
        onClick={() => setIsOpen((open) => !open)}
      >
        <span className="usermenu-avatar" aria-hidden="true">
          {name.slice(0, 1).toUpperCase()}
        </span>
        <span className="usermenu-name-text">{name}</span>
        <CaretDown
          size={12}
          weight="bold"
          className={"usermenu-chevron" + (isOpen ? " is-open" : "")}
          aria-hidden="true"
        />
      </button>

      <div className="usermenu-panel" role="menu" hidden={!isOpen}>
        <div className="usermenu-header">
          <div className="usermenu-name">
            {name}
            {isExpert && <span className="role-badge is-expert">专家</span>}
          </div>
          <div className="usermenu-email">{v2User?.email}</div>
        </div>
        <Link
          role="menuitem"
          className="usermenu-item"
          to="/profile"
          onClick={() => setIsOpen(false)}
        >
          <UserCircle size={16} aria-hidden="true" />
          个人中心
        </Link>
        {isExpert && (
          <>
            <Link
              role="menuitem"
              className="usermenu-item"
              to="/my-experts"
              onClick={() => setIsOpen(false)}
            >
              <Users size={16} aria-hidden="true" />
              我的专家
            </Link>
            <Link
              role="menuitem"
              className="usermenu-item"
              to="/skills"
              onClick={() => setIsOpen(false)}
            >
              <Wrench size={16} aria-hidden="true" />
              Skill 管理
            </Link>
          </>
        )}
        {v2User && (
          <Link
            role="menuitem"
            className="usermenu-item"
            to="/settings/providers"
            onClick={() => setIsOpen(false)}
          >
            <Plug size={16} aria-hidden="true" />
            Provider 设置
          </Link>
        )}
        {v2User && (
          <button
            type="button"
            role="menuitem"
            className="usermenu-item"
            onClick={handleV2Logout}
          >
            <DoorOpen size={16} aria-hidden="true" />
            退出账户会话
          </button>
        )}
      </div>
    </div>
  );
}

export default function NavBar() {
  const { v2User } = useAuth();

  return (
    <header className="navbar">
      <div className="navbar-inner">
        <Link to="/" className="navbar-brand" aria-label="AgentCraft 首页">
          <span className="navbar-mark" aria-hidden="true" />
          AgentCraft
        </Link>
        <nav className="navbar-links" aria-label="主导航">
          <NavLink to="/" end className="navbar-link">
            首页
          </NavLink>
          {TASK_DOMAIN_LINKS.map(({ to, label }) => (
            <NavLink key={to} to={to} className="navbar-link">
              {label}
            </NavLink>
          ))}
          {/* T12a：管理控制台入口——仅 V2 admin 可见（V1 role 与 admin 面无关） */}
          {v2User?.role === "admin" && (
            <NavLink to="/admin" className="navbar-link">
              管理
            </NavLink>
          )}
        </nav>
        <div className="navbar-actions">
          <ThemeToggle />
          {v2User ? (
            <UserMenu />
          ) : (
            <Link to="/login" className="btn btn-primary navbar-login">
              登录
            </Link>
          )}
        </div>
      </div>
    </header>
  );
}
