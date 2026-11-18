import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: [
      {
        find: /^dayjs$/,
        replacement: "dayjs/esm/index.js",
      },
    ],
  },
  build: {
    commonjsOptions: {
      exclude: [/dayjs/, /lodash-es/],
    },
  },
  server: { host: "0.0.0.0", port: 5173 },
});
