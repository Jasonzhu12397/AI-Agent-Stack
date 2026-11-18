import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const viteConfig = new URL("../vite.config.ts", import.meta.url);

test("Vite resolves Day.js core and plugin subpaths to valid ESM entrypoints", async () => {
  const source = await readFile(viteConfig, "utf8");
  assert.ok(source.includes("find: /^dayjs\\/plugin\\/(.+)\\.js$/"));
  assert.ok(source.includes("find: /^dayjs\\/plugin\\/(.+)$/"));
  assert.ok(source.includes('replacement: "dayjs/esm/plugin/$1/index.js"'));
  assert.ok(source.includes('replacement: "dayjs/esm/index.js"'));
});

test("Vite excludes ESM-only dependencies from CommonJS conversion", async () => {
  const source = await readFile(viteConfig, "utf8");
  const exclusions = source.split("exclude: [", 2)[1]?.split("]", 1)[0] ?? "";
  for (const packageName of [
    "dayjs",
    "lodash-es",
    "vue",
    "@vue",
    "@vueuse",
    "@element-plus",
    "memoize-one",
    "async-validator",
    "@ctrl/tinycolor",
  ]) {
    assert.ok(exclusions.includes(packageName), `Missing CommonJS exclusion for ${packageName}`);
  }
  assert.ok(source.includes("commonjsOptions"));
});
