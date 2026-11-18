import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const viteConfig = new URL("../vite.config.ts", import.meta.url);

test("Vite resolves Day.js core and plugins to ESM and excludes ESM packages from CommonJS conversion", async () => {
  const source = await readFile(viteConfig, "utf8");
  assert.match(source, /find:\s*\/\^dayjs\\\/plugin\\\//);
  assert.ok(source.includes("dayjs/esm/plugin/$1/index.js"));
  assert.ok(source.includes('replacement: "dayjs/esm/index.js"'));
  assert.ok(source.includes("/node_modules\\/vue\\//"));
  assert.ok(source.includes("/node_modules\\/@vue\\//"));
  assert.ok(source.includes("/node_modules\\/lodash-es\\//"));
  assert.ok(source.includes("commonjsOptions"));
});
