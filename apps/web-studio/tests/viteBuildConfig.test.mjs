import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const viteConfig = new URL("../vite.config.ts", import.meta.url);

test("Vite resolves Day.js core and plugin subpaths to valid ESM entrypoints", async () => {
  const source = await readFile(viteConfig, "utf8");
  assert.ok(source.includes("dayjs/esm/plugin/$1/index.js"));
  assert.ok(source.includes("dayjs/esm/index.js"));
  assert.match(source, /find:\s*\/\^dayjs/);
});

test("Vite leaves Vue ecosystem ESM packages out of CommonJS conversion", async () => {
  const source = await readFile(viteConfig, "utf8");
  assert.ok(source.includes("/node_modules\\/vue\\//"));
  assert.ok(source.includes("/node_modules\\/@vue\\//"));
  assert.ok(source.includes("/node_modules\\/@vueuse\\//"));
  assert.ok(source.includes("/node_modules\\/@element-plus\\/icons-vue\\//"));
  assert.ok(source.includes("/node_modules\\/lodash-es\\//"));
  assert.ok(source.includes("commonjsOptions"));
});
