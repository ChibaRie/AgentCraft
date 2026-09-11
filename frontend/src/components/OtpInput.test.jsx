import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import OtpInput from "./OtpInput.jsx";

/**
 * 受控 harness：父态原样存储 onChange 值（模拟 LoginPage 的 setOtp），
 * output 外显当前值便于断言 DOM 与受控值同步。
 */
function renderOtp() {
  const changeSpy = vi.fn();
  function Harness() {
    const [value, setValue] = useState("");
    return (
      <>
        <OtpInput
          value={value}
          onChange={(next) => {
            changeSpy(next);
            setValue(next);
          }}
        />
        <output>{`值[${value}]`}</output>
      </>
    );
  }
  render(<Harness />);
  const input = screen.getByLabelText("两步验证码");
  return { input, changeSpy };
}

describe("OtpInput 原语", () => {
  it("aria-label 钉死「两步验证码」且挂载即自动聚焦", () => {
    const { input } = renderOtp();
    expect(input.getAttribute("aria-label")).toBe("两步验证码");
    expect(document.activeElement).toBe(input);
  });

  it("输入过滤非数字字符", () => {
    const { input, changeSpy } = renderOtp();
    fireEvent.change(input, { target: { value: "12ab34" } });
    expect(changeSpy).toHaveBeenLastCalledWith("1234");
    expect(input.value).toBe("1234");
    expect(screen.getByText("值[1234]")).toBeTruthy();
  });

  it("超长截断到 8 位（后端 totp_code 6-8 契约）", () => {
    const { input, changeSpy } = renderOtp();
    fireEvent.change(input, { target: { value: "1234567890" } });
    expect(changeSpy).toHaveBeenLastCalledWith("12345678");
    expect(input.value).toBe("12345678");
  });

  it("粘贴：过滤非数字并整体替换当前值", () => {
    const { input, changeSpy } = renderOtp();
    fireEvent.change(input, { target: { value: "11" } });
    fireEvent.paste(input, {
      clipboardData: { getData: () => "9x9-2" },
    });
    expect(changeSpy).toHaveBeenLastCalledWith("992");
    expect(input.value).toBe("992");
  });

  it("粘贴超长同样截断到 8 位", () => {
    const { input, changeSpy } = renderOtp();
    fireEvent.paste(input, {
      clipboardData: { getData: () => "123456789012" },
    });
    expect(changeSpy).toHaveBeenLastCalledWith("12345678");
  });
});
