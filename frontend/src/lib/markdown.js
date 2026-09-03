/**
 * 轻量 Markdown 渲染（零依赖，XSS 安全）。
 *
 * 策略：先整体 HTML 转义，再在转义后的安全文本上做有限结构替换，
 * 因此用户内容中的 <script>/onerror 等永远以字面量呈现。
 * 支持子集（对齐专家回复的常见形态）：围栏代码块、行内代码、
 * 粗体/斜体、标题、无序/有序列表、引用、链接、换行。
 */

/** HTML 转义：& < > " ' 全覆盖。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/** 行内规则：代码 → 粗体 → 斜体 → 链接。输入必须是已转义文本。 */
function renderInline(escaped) {
  const codes = [];
  // 先摘出行内代码，代码内容不做后续替换
  let text = escaped.replace(/`([^`]+)`/g, (_match, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });
  // 链接：[文本](http(s)://…) —— 只放行 http(s)，href 已随整体转义
  text = text.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
  );
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  return text.replaceAll(/\u0000(\d+)\u0000/g, (_m, index) => `<code>${codes[Number(index)]}</code>`);
}

/**
 * 渲染 Markdown 为 HTML 字符串（已转义，可安全 innerHTML）。
 */
export function renderMarkdown(source) {
  const text = String(source ?? "").replace(/\r\n/g, "\n");
  if (!text.trim()) {
    return "";
  }
  const blocks = [];
  const segments = text.split(/```/);
  // 奇数段是围栏代码块
  segments.forEach((segment, index) => {
    if (index % 2 === 1) {
      const newline = segment.indexOf("\n");
      const body = newline === -1 ? "" : segment.slice(newline + 1);
      const language = newline === -1 ? segment.trim() : segment.slice(0, newline).trim();
      const code = body.replace(/\n$/, "");
      blocks.push(
        `<pre class="md-code">${language ? `<span class="md-code-lang">${escapeHtml(language)}</span>` : ""}<code>${escapeHtml(code)}</code></pre>`,
      );
      return;
    }
    const lines = segment.split("\n");
    let listBuffer = null; // "ul" | "ol" | null
    let quoteBuffer = [];

    const flushList = () => {
      if (listBuffer) {
        blocks.push(`<${listBuffer} class="md-list">${quoteBuffer.join("")}</${listBuffer}>`);
        listBuffer = null;
        quoteBuffer = [];
      }
    };

    for (const rawLine of lines) {
      const line = escapeHtml(rawLine);
      const listItem = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
      if (listItem) {
        const ordered = /^\s*\d/.test(rawLine);
        if (listBuffer && listBuffer !== (ordered ? "ol" : "ul")) {
          flushList();
        }
        listBuffer = ordered ? "ol" : "ul";
        quoteBuffer.push(`<li>${renderInline(listItem[1])}</li>`);
        continue;
      }
      flushList();
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        const level = heading[1].length + 2; // ## → h4，避免与页面标题层级竞争
        blocks.push(`<h${level} class="md-heading">${renderInline(heading[2])}</h${level}>`);
        continue;
      }
      const quote = line.match(/^&gt;\s?(.*)$/);
      if (quote) {
        blocks.push(`<blockquote class="md-quote">${renderInline(quote[1])}</blockquote>`);
        continue;
      }
      if (line.trim() === "") {
        continue;
      }
      blocks.push(`<p class="md-p">${renderInline(line)}</p>`);
    }
    flushList();
  });
  return blocks.join("");
}
