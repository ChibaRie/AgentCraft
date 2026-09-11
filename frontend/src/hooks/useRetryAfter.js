/**
 * 429 Retry-After 倒计时（FE-T3 引入于 LoginPage，FE-T4 提为共享）。
 * start(seconds) 置初值，每秒递减，归零自动解除禁用。
 */
import { useCallback, useEffect, useState } from "react";

export function useRetryAfter() {
  const [secondsLeft, setSecondsLeft] = useState(0);

  useEffect(() => {
    if (secondsLeft <= 0) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      setSecondsLeft((current) => Math.max(0, current - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [secondsLeft]);

  const start = useCallback((seconds) => {
    const parsed = Number(seconds);
    setSecondsLeft(Number.isFinite(parsed) && parsed > 0 ? Math.ceil(parsed) : 0);
  }, []);

  return { retryAfter: secondsLeft, start };
}
