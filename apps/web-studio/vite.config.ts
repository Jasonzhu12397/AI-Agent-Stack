import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  build: {
    commonjsOptions: {
      exclude: [/dayjs/, /vue/, /@vue/],
    },
  },
  server: { host: "0.0.0.0", port: 5173 },
});
