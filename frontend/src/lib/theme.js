const THEME_KEY = "agentcraft_theme";

/**
 * 主题切换（日间/夜间）：
 * - 选择持久化到 localStorage（"light" | "dark"）
 * - 未选择时跟随系统 prefers-color-scheme
 * - index.html 的内联脚本在首帧前写入 <html data-theme>，避免主题闪烁
 */
export function getStoredTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    return null;
  }
}

export function getEffectiveTheme() {
  const stored = getStoredTheme();
  if (stored) {
    return stored;
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
}

export function setTheme(theme) {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // 存储不可用时仅本次会话生效
  }
  applyTheme(theme);
}

export function toggleTheme() {
  const next = getEffectiveTheme() === "dark" ? "light" : "dark";
  setTheme(next);
  return next;
}
