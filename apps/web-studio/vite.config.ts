import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: [
      {
        find: /^dayjs\/plugin\/(.*)$/,
        replacement: "dayjs/esm/plugin/$1/index.js",
      },
      {
        find: /^dayjs$/,
        replacement: "dayjs/esm/index.js",
      },
    ],
  },
  build: {
    commonjsOptions: {
      exclude: [
        /node_modules\/dayjs\/esm\//,
        /node_modules\/lodash-es\//,
        /node_modules\/vue\//,
        /node_modules\/@vue\//,
      ],
    },
  },
  server: { host: "0.0.0.0", port: 5173 },
});
