import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  build: {
    commonjsOptions: {
      exclude: [
        /node_modules\\/dayjs\\//,
        /node_modules\\/vue\\//,
        /node_modules\\/@vue\\//,
      ],
    },
  },
  server: { host: "0.0.0.0", port: 5173 },
});
