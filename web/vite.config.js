import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base=/ui/：构建产物由 FastAPI 同源挂载在 /ui 下，资源路径必须从根算起。
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8766",
    },
  },
});