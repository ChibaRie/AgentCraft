import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 测试基建（FE-T1）：jsdom + globals；FE-T2 追加 react 插件——组件/Provider 的
// JSX 在测试环境同样走 automatic runtime（否则经典转换缺 React 导入报错）。
// 版本纪律：vitest 锁 3.x（^3.2.7）——vitest 4/5 的 mandatory peer vite ^6.4+
// 与本仓 Vite 5.4 冲突；未来升级 vitest 须同步升 Vite ≥6.4（独立任务）。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
  },
});
