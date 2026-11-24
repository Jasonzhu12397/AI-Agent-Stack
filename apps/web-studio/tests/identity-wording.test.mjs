import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

// The console is a multi-agent platform: Cloud Control, Target and EPH agents.
// A "dual-agent preview" badge used to sit in the shell header, which misled
// users about what the product is. This guard keeps that wording from creeping
// back into any source file or translation table.
const ROOT = new URL("../src/", import.meta.url);
const BANNED = /dual-agent\s*preview|双智能体预览/i;

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (/\.(vue|ts|mjs|css|json)$/.test(entry)) out.push(full);
  }
  return out;
}

test("test_shell_has_no_dual_agent_preview_wording", () => {
  const offenders = walk(ROOT.pathname).filter((file) => BANNED.test(readFileSync(file, "utf8")));
  assert.deepEqual(offenders, []);
});

test("test_shell_header_shows_build_version", () => {
  const shell = readFileSync(new URL("../src/App.vue", import.meta.url), "utf8");
  assert.match(shell, /v\{\{\s*appVersion\s*\}\}/);
  assert.match(shell, /const appVersion = __APP_VERSION__/);
});
