import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: [
      {
        find: /^dayjs\/plugin\/(.+)\.js$/,
        replacement: "dayjs/esm/plugin/$1/index.js",
      },
      {
        find: /^dayjs\/plugin\/(.+)$/,
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
        /node_modules\/@vueuse\//,
        /node_modules\/@element-plus\/icons-vue\//,
        /node_modules\/memoize-one\//,
        /node_modules\/async-validator\//,
        /node_modules\/@ctrl\/tinycolor\//,
      ],
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    // The console and the API share one origin in hosted previews, which
    // removes CORS and keeps session cookies first-party.
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
});
