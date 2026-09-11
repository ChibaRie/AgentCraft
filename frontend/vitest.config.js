import { defineConfig } from "vitest/config";

// 测试基建（FE-T1）：jsdom + globals。
// 版本纪律：vitest 锁 3.x（^3.2.7）——vitest 4/5 的 mandatory peer vite ^6.4+
// 与本仓 Vite 5.4 冲突；未来升级 vitest 须同步升 Vite ≥6.4（独立任务）。
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
  },
});
