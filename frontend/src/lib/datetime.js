/**
 * 后端以 UTC 存储时间但序列化为无时区的 naive ISO 串；补 Z 避免被当作本地时间。
 */
export function formatDateTime(isoString) {
  if (!isoString) {
    return "";
  }
  const normalized = /[zZ]|[+-]\d{2}:?\d{2}$/.test(isoString) ? isoString : `${isoString}Z`;
  return new Date(normalized).toLocaleString("zh-CN", { hour12: false });
}
