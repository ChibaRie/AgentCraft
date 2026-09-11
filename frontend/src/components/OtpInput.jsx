import { useEffect, useRef, useState } from "react";

const OTP_MAX_LENGTH = 8;
const NON_DIGIT_PATTERN = /\D/g;

/**
 * TOTP 验证码输入原语（FE-T3）。
 *
 * 后端契约（只读参考）：backend/api/v2/schemas.py MfaVerifyRequest.totp_code
 * 6-8 位数字——输入端过滤非数字并截断到 8 位上限。
 *
 * - 受控组件 value/onChange（父态即真值，本组件不持有 value 状态）；
 * - 粘贴：过滤非数字后整体替换当前值（TOTP 整码粘贴是主路径）；
 * - 挂载自动聚焦（MFA 挑战卡片就地切换出现时即待输入）；
 * - 结构为单个透明输入覆盖 8 个等宽分格（v2.css .otp-cell）：分格呈现字符，
 *   输入层接收键入——受控过滤天然无分格焦点管理的复杂度。
 */
export default function OtpInput({ value, onChange }) {
  const inputRef = useRef(null);
  const [isFocused, setIsFocused] = useState(false);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  function applyNext(rawValue) {
    const filtered = rawValue.replace(NON_DIGIT_PATTERN, "").slice(0, OTP_MAX_LENGTH);
    // 过滤后受控值不变时 React 不会重渲染，DOM 会残留非法字符——手动回写同步
    if (inputRef.current && inputRef.current.value !== filtered) {
      inputRef.current.value = filtered;
    }
    onChange(filtered);
  }

  return (
    <div className="v2-otp">
      <div className="v2-otp-cells" aria-hidden="true">
        {Array.from({ length: OTP_MAX_LENGTH }, (_, index) => {
          const classNames = ["otp-cell"];
          if (value[index]) {
            classNames.push("is-filled");
          }
          if (isFocused && index === Math.min(value.length, OTP_MAX_LENGTH - 1)) {
            classNames.push("is-active");
          }
          return (
            <span key={index} className={classNames.join(" ")}>
              {value[index] ?? ""}
            </span>
          );
        })}
      </div>
      <input
        ref={inputRef}
        className="v2-otp-input"
        aria-label="两步验证码"
        inputMode="numeric"
        autoComplete="one-time-code"
        maxLength={OTP_MAX_LENGTH}
        value={value}
        onChange={(event) => applyNext(event.target.value)}
        onPaste={(event) => {
          event.preventDefault();
          const pasted = event.clipboardData?.getData("text") ?? "";
          applyNext(pasted);
        }}
        onFocus={() => setIsFocused(true)}
        onBlur={() => setIsFocused(false)}
      />
    </div>
  );
}
