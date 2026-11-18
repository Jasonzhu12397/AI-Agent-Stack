import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const viteConfig = new URL("../vite.config.ts", import.meta.url);

test("Vite resolves Element Plus Day.js imports to ESM and excludes ESM dependencies from CommonJS conversion", async () => {
  const source = await readFile(viteConfig, "utf8");
  assert.match(source, /find:\s*\/\^dayjs\$\//);
  assert.ok(source.includes('replacement: "dayjs/esm/index.js"'));
  assert.ok(source.includes("exclude: [/dayjs/, /lodash-es/]"));
  assert.ok(source.includes("commonjsOptions"));
});
