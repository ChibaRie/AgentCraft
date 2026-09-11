/**
 * 重发验证邮件编排（FE-T4）：POST V2_AUTH/email-verification/resend。
 *
 * - 认证端点：requestV2 自动携带 CSRF 双提交头；
 * - 60s 前端冷却：点击瞬间乐观启动（防连点），非 429 失败同样冷却；
 * - 429：服务端 Retry-After 秒数覆盖前端冷却（服务端为准）；无 Retry-After 头
 *   时保留既有冷却（不发明默认秒数——T3 裁决延续）；
 * - sent / error：成功提示（role=status）与失败原因（role=alert）由调用方渲染。
 */
import { useCallback, useState } from "react";
import { V2ApiError, requestV2 } from "../api/v2/client.js";
import { V2_AUTH } from "../api/v2/routes.js";
import { useRetryAfter } from "./useRetryAfter.js";

/** 前端冷却：与后端 resend 限流窗口对齐的保守下限 */
const RESEND_COOLDOWN_SECONDS = 60;

export function useResendVerification() {
  const { retryAfter, start } = useRetryAfter();
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState("");

  const resend = useCallback(async () => {
    if (sending || retryAfter > 0) {
      return false;
    }
    setSending(true);
    setSent(false);
    setError("");
    start(RESEND_COOLDOWN_SECONDS);
    try {
      await requestV2(`${V2_AUTH}/email-verification/resend`, { method: "POST" });
      setSent(true);
      return true;
    } catch (caught) {
      if (caught?.status === 429 && caught.retryAfter > 0) {
        start(caught.retryAfter);
      }
      // 仅 V2ApiError 携带面向用户的 message；网络层裸错误（TypeError 等）用兜底文案
      setError(caught instanceof V2ApiError ? caught.message : "重发失败，请稍后重试");
      return false;
    } finally {
      setSending(false);
    }
  }, [sending, retryAfter, start]);

  return { resend, cooldown: retryAfter, sending, sent, error };
}
