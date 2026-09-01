const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

export async function request(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const response = await fetch(BASE_URL + path, { ...options, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(payload?.detail || "HTTP " + response.status);
  }
  return payload;
}
