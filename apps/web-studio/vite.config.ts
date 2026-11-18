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
      exclude: [/node_modules\/dayjs\/esm\//],
    },
  },
  server: { host: "0.0.0.0", port: 5173 },
});
