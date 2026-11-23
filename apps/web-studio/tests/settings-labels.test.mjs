import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { SETTINGS_LABELS } from "../src/settings/shared/scope.ts";
import { pageLabels as accountLabels } from "../src/settings/account/logic.ts";
import { pageLabels as generalLabels } from "../src/settings/general/logic.ts";

const settingsDir = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "i18n", "settings");

/** Mirrors the merge in src/i18n/settings/index.ts without relying on extensionless ESM resolution. */
function loadMergedMessages() {
  const merged = {};
  for (const file of readdirSync(settingsDir).filter((name) => name.endsWith(".zh.ts"))) {
    const source = readFileSync(join(settingsDir, file), "utf8");
    const entry = /"([^"]+)":\s*"([^"]*)"/g;
    let match;
    while ((match = entry.exec(source))) {
      const key = match[1].replace(/\s+/g, " ").trim();
      if (merged[key] !== undefined && merged[key] !== match[2]) {
        throw new Error(`Conflicting settings translation for "${key}"`);
      }
      merged[key] = match[2];
    }
  }
  return merged;
}

const settingsMessages = loadMergedMessages();

function assertCovered(labels, scope) {
  for (const source of Object.keys(labels)) {
    assert.ok(
      Object.prototype.hasOwnProperty.call(settingsMessages, source),
      `${scope}: missing zh translation for "${source}"`,
    );
  }
}

test("every settings nav label has a Chinese translation in the merged namespaces", () => {
  for (const label of Object.values(SETTINGS_LABELS)) {
    assert.ok(
      Object.prototype.hasOwnProperty.call(settingsMessages, label),
      `missing zh translation for "${label}"`,
    );
  }
});

test("account page labels are fully covered by the merged dictionary", () => {
  assertCovered(accountLabels, "account");
});

test("general page labels are fully covered by the merged dictionary", () => {
  assertCovered(generalLabels, "general");
});
