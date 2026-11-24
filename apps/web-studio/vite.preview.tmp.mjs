import { defineConfig } from "vite";

export default defineConfig({
  root: process.cwd(),
  build: { outDir: "dist" },
  preview: {
    port: 4173,
    host: "127.0.0.1",
    proxy: {
      "/api": { target: "http://127.0.0.1:8001", changeOrigin: false },
    },
  },
});
