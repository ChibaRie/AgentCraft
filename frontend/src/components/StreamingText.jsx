import { useEffect, useRef, useState } from "react";

const CJK_RE = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/u;

function isCjk(char) {
  return CJK_RE.test(char);
}

/**
 * 把文本切成可换行的渲染单元：
 * - 空白保持为裸文本，交给父级 white-space: pre-wrap 处理换行；
 * - CJK 单字成块，允许中文在任意字间断行；
 * - 拉丁/数字连续串成词，避免英文单词被逐字拆开。
 *
 * 输出仍与原文 textContent 完全一致；这里只负责视觉切分，不改变语义。
 */
function tokenize(text) {
  const chars = Array.from(text);
  const tokens = [];
  let index = 0;

  while (index < chars.length) {
    const char = chars[index];

    if (/\s/u.test(char)) {
      let end = index + 1;
      while (end < chars.length && /\s/u.test(chars[end])) {
        end += 1;
      }
      tokens.push({ kind: "gap", text: chars.slice(index, end).join(""), index });
      index = end;
      continue;
    }

    if (isCjk(char)) {
      tokens.push({ kind: "cjk", text: char, index });
      index += 1;
      continue;
    }

    let end = index + 1;
    while (
      end < chars.length &&
      !/\s/u.test(chars[end]) &&
      !isCjk(chars[end])
    ) {
      end += 1;
    }
    tokens.push({ kind: "word", text: chars.slice(index, end).join(""), index });
    index = end;
  }

  return tokens;
}

/**
 * 流式正文：每个新字符从 blur(6px) 过渡到清晰（90ms），
 * 相邻字错开 30ms。全部字符始终留在 DOM 中，读屏与复制拿到完整文本；
 * 动画只作用于新增字符，不重播已出现内容。
 */
export default function StreamingText({ text }) {
  const previousRef = useRef("");
  const [animateRange, setAnimateRange] = useState({ from: 0, to: 0 });
  const count = Array.from(text ?? "").length;

  useEffect(() => {
    const previous = previousRef.current;
    const next = text ?? "";
    const previousCount = Array.from(previous).length;
    if (next.startsWith(previous) && count > previousCount) {
      setAnimateRange({ from: previousCount, to: count });
    } else if (!next.startsWith(previous)) {
      setAnimateRange({ from: 0, to: count });
    }
    previousRef.current = next;
  }, [text, count]);

  if (!text) {
    return null;
  }

  const tokens = tokenize(text);

  return tokens.map((token) => {
    if (token.kind === "gap") {
      return (
        <span className="stream-gap" key={`gap-${token.index}`}>
          {token.text}
        </span>
      );
    }

    const chars = Array.from(token.text);
    return (
      <span className="stream-word" key={`${token.kind}-${token.index}`}>
        {chars.map((char, offset) => {
          const absolute = token.index + offset;
          const isNew = absolute >= animateRange.from && absolute < animateRange.to;
          return (
            <span
              className={"stream-char" + (isNew ? " is-new" : "")}
              key={`${token.index}-${offset}`}
              style={
                isNew
                  ? { "--char-delay": `${Math.min((absolute - animateRange.from) * 30, 600)}ms` }
                  : undefined
              }
            >
              {char}
            </span>
          );
        })}
      </span>
    );
  });
}
