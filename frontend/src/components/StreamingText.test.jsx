import { act, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import StreamingText from "./StreamingText.jsx";

function flush() {
  return act(async () => {});
}

describe("StreamingText", () => {
  it("完整文本始终在 DOM 中；新字符带 90ms/30ms 节奏标记，旧字符不重播", async () => {
    const { container, rerender } = render(<StreamingText text="你好" />);
    await flush();

    expect(container.textContent).toBe("你好");
    const first = container.querySelectorAll(".stream-char.is-new");
    expect(first).toHaveLength(2);
    expect(first[1].style.getPropertyValue("--char-delay")).toBe("30ms");

    rerender(<StreamingText text="你好，世界" />);
    await flush();

    expect(container.textContent).toBe("你好，世界");
    const fresh = [...container.querySelectorAll(".stream-char.is-new")];
    expect(fresh.map((node) => node.textContent).join("")).toBe("，世界");
    expect(container.querySelectorAll(".stream-char")).toHaveLength(5);
  });

  it("空文本不渲染，也不抛错", () => {
    const { container } = render(<StreamingText text="" />);
    expect(container.textContent).toBe("");
  });
});
