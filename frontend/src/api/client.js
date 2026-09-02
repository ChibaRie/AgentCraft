const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const TOKEN_KEY = "agentcraft_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
  } else {
    localStorage.removeItem(TOKEN_KEY);
  }
}

/**
 * 统一 API 请求：注入 Bearer Token，解析 {data} / {error:{code,message}} 信封。
 * 成功返回完整响应体（调用方按需取 .data / .total），失败抛出携带 code 的错误。
 */
export async function request(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const token = getToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(BASE_URL + path, { ...options, headers });
  const payload = await response.json().catch(() => null);

  if (!response.ok) {
    const error = new Error(payload?.error?.message || "HTTP " + response.status);
    error.code = payload?.error?.code || "HTTP_" + response.status;
    error.status = response.status;
    throw error;
  }
  if (payload === null) {
    // 2xx 但响应体不是 JSON（如静态托管的 SPA 回退页）
    const error = new Error("响应格式错误，请稍后重试");
    error.code = "INVALID_RESPONSE";
    error.status = response.status;
    throw error;
  }
  return payload;
}
