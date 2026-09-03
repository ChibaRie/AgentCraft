const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const TOKEN_KEY = "agentcraft_token";

/**
 * 解析单个 SSE 帧：event: 行 + data: 行，空行分隔（Engineering Spec §6.6）。
 */
function parseFrame(block) {
  let name = "message";
  let data = "";
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      name = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      data += line.slice(5).trim();
    }
  }
  if (!data) {
    return null;
  }
  try {
    return { event: name, data: JSON.parse(data) };
  } catch {
    return null;
  }
}

/**
 * SSE 客户端（Engineering Spec §8.3）：Fetch + ReadableStream 逐块解析
 * POST /api/tasks/{id}/messages 的事件流。
 * 不用 EventSource——它仅支持 GET 且无法携带 Authorization 头。
 */
export class SseClient {
  constructor(path) {
    this.path = path;
    this.controller = new AbortController();
  }

  async *connect(content) {
    const token = localStorage.getItem(TOKEN_KEY);
    const response = await fetch(BASE_URL + this.path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ content }),
      signal: this.controller.signal,
    });

    if (!response.ok || !response.body) {
      // 前置校验失败（403/404/409 等）返回统一 JSON 错误信封而非事件流
      const payload = await response.json().catch(() => null);
      const error = new Error(payload?.error?.message || "HTTP " + response.status);
      error.code = payload?.error?.code || "HTTP_" + response.status;
      error.status = response.status;
      throw error;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const event = parseFrame(buffer.slice(0, boundary));
        if (event) {
          yield event;
        }
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
  }

  abort() {
    this.controller.abort();
  }
}
