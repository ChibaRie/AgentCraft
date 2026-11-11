import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Collapsible from "./Collapsible.jsx";

describe("Collapsible", () => {
  it("内容保持挂载；0fr 与 1fr 只由 is-open 切换，不测量高度", () => {
    const { container } = render(
      <Collapsible title="输入文件">
        <p>文件清单</p>
      </Collapsible>
    );

    const trigger = screen.getByRole("button", { name: "输入文件" });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".collapse").classList.contains("is-open")).toBe(false);

    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector(".collapse").classList.contains("is-open")).toBe(true);
    expect(screen.getByText("文件清单")).toBeTruthy();
  });
});
