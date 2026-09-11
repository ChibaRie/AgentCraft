/**
 * 双轨身份显示名（E4）：修复 V2 user 形状下 `user.username.*` 的 TypeError 白屏
 * （无 ErrorBoundary，渲染点直接调用，不做调用方判空）。
 *
 * V1 user 有 username；V2 user（users/me 形状）无 username，以 email 前缀兜底。
 */
export function displayName(user) {
  return user?.username ?? (user?.email ? user.email.split("@")[0] : "");
}
