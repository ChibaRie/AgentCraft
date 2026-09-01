const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

export async function* streamSse(path, { method = "POST", headers = {}, body } = {}) {
  const response = await fetch(BASE_URL + path, {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok || !response.body) {
    throw new Error("SSE connection failed: HTTP " + response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      const event = { event: "message", data: "" };
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event.event = line.slice(6).trim();
        if (line.startsWith("data:")) event.data += line.slice(5).trim();
      }
      if (event.data) yield event;
    }
  }
}
