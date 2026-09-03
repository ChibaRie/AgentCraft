import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import {
  CaretDown,
  Moon,
  Plug,
  SignOut,
  Sun,
  UserCircle,
  Users,
  Wrench,
} from "@phosphor-icons/react";
import { useAuth } from "../auth/AuthContext.jsx";
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

function UserMenu() {
  const { user, isExpert, logout } = useAuth();
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

  function handleLogout() {
    setIsOpen(false);
    logout();
    navigate("/login");
  }

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
          {user.username.slice(0, 1).toUpperCase()}
        </span>
        <span className="usermenu-name-text">{user.username}</span>
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
            {user.username}
            {isExpert && <span className="role-badge is-expert">专家</span>}
          </div>
          <div className="usermenu-email">{user.email}</div>
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
            <Link
              role="menuitem"
              className="usermenu-item"
              to="/settings/providers"
              onClick={() => setIsOpen(false)}
            >
              <Plug size={16} aria-hidden="true" />
              Provider 设置
            </Link>
          </>
        )}
        <button type="button" role="menuitem" className="usermenu-item" onClick={handleLogout}>
          <SignOut size={16} aria-hidden="true" />
          登出
        </button>
      </div>
    </div>
  );
}

export default function NavBar() {
  const { isAuthenticated } = useAuth();

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
          <NavLink to="/discover" className="navbar-link">
            专家中心
          </NavLink>
          <NavLink to="/tasks" className="navbar-link">
            任务
          </NavLink>
        </nav>
        <div className="navbar-actions">
          <ThemeToggle />
          {isAuthenticated ? (
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
