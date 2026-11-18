import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const viteConfig = new URL("../vite.config.ts", import.meta.url);

test("Vite does not send ESM-only Vue and Day.js packages through CommonJS transform", async () => {
  const source = await readFile(viteConfig, "utf8");
  assert.match(source, /commonjsOptions/);
  assert.match(source, /node_modules\\\\\\/dayjs\\\\\\//);
  assert.match(source, /node_modules\\\\\\/vue\\\\\\//);
  assert.match(source, /node_modules\\\\\\/@vue\\\\\\//);
  assert.doesNotMatch(source, /replacement:\\s*["']dayjs\\/esm\\/index\\.js/);
});
